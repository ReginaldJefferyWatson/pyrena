#!/usr/bin/env python3
"""
Before running this script (The Tournament Scheduler), you must have:
- Setup a postgres server and created the tables a la https://github.com/siggame/ophelia/blob/develop/db/init.sql
- Installed the psycopg2 python3 library (for postgres access)
- Passed in all relevant ENVIRONMENT_VARIABLES below
"""

# pip install psycopg2
import psycopg2
import psycopg2.extras

# Builtin libraries
import base64
import collections
import datetime
import itertools
import json
import logging
import math
import os
import pprint
import random
import shutil
import signal
import string
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Tournament_scheduler is controlled by these environment variables
GAME_NAME = os.getenv('GAME_NAME', 'Chess')  # Capitalization matters!
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'postgres')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASS = os.getenv('DB_PASS', 'postgres')
REFRESH_SECONDS = int(os.getenv('REFRESH_SECONDS', 30))
N_ELIMINATION = int(os.getenv('N_ELIMINATION', 3))
BEST_OF = int(os.getenv('BEST_OF', '7'))

class Submission(object):
    pass
BUY = Submission()
BUY.id = -1
BUY.name = 'BUY'
BUY.version = -1
BUY.status = 'BUY'
BUY.created_at = None

logging.getLogger().setLevel(logging.INFO)

def main():
    logging.info(f'connecting to database "{DB_NAME}" at {DB_USER}@{DB_HOST}:{DB_PORT}')
    conn = psycopg2.connect(dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port=DB_PORT,
            connect_timeout=10,
            cursor_factory=psycopg2.extras.NamedTupleCursor)

    signal.signal(signal.SIGINT, sigint_handler)

    logging.info('Getting latest submissions')
    submissions = get_latest_submissions(conn)

    global nodes
    nodes = list()
    generate_n_elimination_bracket_online(submissions, nodes, N_ELIMINATION)

    try:
        while True:
            update_game_status(conn, [nodes])
            logging.info('Declaring and propogating winners')
            for node in nodes:
                declare_and_propogate_winners(node)
            winner = generate_n_elimination_bracket_online(submissions, nodes, N_ELIMINATION)
            if winner:
                logging.info('Tournament complete')
                logging.info(f'Winner is {winner.winner.name}')
                dot_nodes(nodes)
                break
            create_needed_games(conn, [nodes])
            logging.debug(f'Sleeping {REFRESH_SECONDS}')
            #print_tree([nodes])
            time.sleep(REFRESH_SECONDS)
    except KeyboardInterrupt:
        logging.warning('Caught keyboard interrupt')

def sigint_handler(signal, frame):
    logging.warning('Caught SIGINT')
    global nodes
    dot_nodes(nodes)

def get_latest_submissions(conn):
    cur = conn.cursor()
    q = '''
SELECT s.id, t.name, s.version, s.status, s.created_at
FROM submissions s
INNER JOIN (
    SELECT team_id, MAX(version) as version
    FROM submissions
    WHERE status != 'failed'
    GROUP BY team_id
) m ON
s.team_id = m.team_id AND
s.version = m.version
INNER JOIN teams t
ON s.team_id = t.id
WHERE t.team_captain_id IS NOT NULL
AND t.is_eligible
AND s.status != 'failed'
    '''
    cur.execute(q)
    return cur.fetchall()

class Node:
    def __init__(self):
        self.submissions = list()
        self.feeders = list()
        self.inverted_feeders = list()
        self.games = list()
        self.winner = None
        self.loser = None

        self.winner_child = None
        self.loser_child = None

        self.left_submission = None
        self.right_submission = None
        self.left_feeder = None
        self.right_feeder = None
        self.left_inverted = False
        self.right_inverted = False

def generate_initial_pairing(submissions):
    width = 2**int(math.ceil(math.log2(len(submissions))) - 1)
    shuffled_submissions = list(submissions)
    random.shuffle(shuffled_submissions)
    shuffled_submissions += [BUY] * (2*width - len(shuffled_submissions))
    nodes = [Node() for _ in range(width)]
    i = 0
    for node in nodes:
        node.submissions.append(shuffled_submissions[i])
        i += 1
    for node in nodes:
        node.submissions.append(shuffled_submissions[i])
        i += 1
    return nodes

def generate_single_elimination_bracket(submissions):
    width = 2**int(math.ceil(math.log2(len(submissions))) - 1)
    print(math.ceil(math.log2(len(submissions))))
    level = generate_initial_pairing(submissions)
    levels = [level]
    lower_level = level
    while width > 1:
        width //= 2
        level = list()
        for i in range(width):
            node = Node()
            node.feeders.append(lower_level[i*2])
            node.feeders.append(lower_level[i*2+1])
            level.append(node)
        levels.append(level)
        lower_level = level
    return levels

def generate_double_elimination_bracket(submissions):
    winner_bracket = generate_single_elimination_bracket(submissions)
    # Generate losers bracket
    levels = list()
    previous_level = list()
    left_over = []
    winner_i = 0
    while True:
        winner_level = winner_bracket[winner_i] if winner_i < len(winner_bracket) else []
        feeders = previous_level + winner_level + left_over
        if not feeders:
            break
        if len(feeders) == 1:
            logging.error('Only one feeder!')
            break
        level = list()
        for i in range(0, len(feeders)-1, 2):
            node = Node()
            if i >= len(previous_level):
                node.inverted_feeders.append(feeders[i])
            else:
                node.feeders.append(feeders[i])
            if i+1 >= len(previous_level):
                node.inverted_feeders.append(feeders[i + 1])
            else:
                node.feeders.append(feeders[i + 1])
            level.append(node)
        left_over = [feeders[-1]] if len(feeders) % 2 else []
        levels.append(level)
        previous_level = level
        winner_i += 1
    # combine levels
    combined = list(((a or []) + (b or [])) for a, b in itertools.zip_longest(winner_bracket, [list()] + levels))
    final_match = Node()
    final_match.feeders.append(winner_bracket[-1][0])
    final_match.feeders.append(levels[-1][0])
    final_level = [final_match]
    combined.append(final_level)
    return combined

# Must be called continuosly as winners are updated
# When the tournament is finished, returns the winner node
# Otherwise returns false
def generate_n_elimination_bracket_online(submissions, nodes, max_losses):
    wins = collections.defaultdict(lambda: 0)
    losses = collections.defaultdict(lambda: 0)
    if not nodes:
        nodes.extend(generate_initial_pairing(submissions))
    for node in nodes:
        if node.loser:
            losses[node.loser] += 1
        if node.winner:
            wins[node.winner] += 1
    available = list()
    pending_matches = False
    for node in nodes:
        if node.winner and not node.winner_child:
            available.append((node, node.winner))
        if node.loser and not node.loser_child:
            if losses[node.loser] < max_losses:
                available.append((node, node.loser))
        if not node.winner:
            pending_matches = True
    # Finished!
    if not pending_matches and len(available) == 1:
        return available[0][0]
    if not pending_matches and len(available) == 0:
        logging.error('No matches, and no available players!')
        return nodes[-1]
    print(pending_matches)
    print(len(available))
    # Try to balance the matches so that teams progress at an even rate through the bracket
    available_by_score = collections.defaultdict(lambda: list())
    for node, who in available:
        available_by_score[(losses[who], wins[who])].append((node, who))
    available_by_losses = collections.defaultdict(lambda: list())
    for node, who in available:
        available_by_losses[losses[who]].append((node, who))
    groups = list()
    groups.append(available_by_score)
    groups.append(available_by_losses)
    groups.append({0: list(sorted(available, key=lambda p: -losses[p[1]]))})
    for group in groups:
        print('group', len(group))
        for k, node_sources in group.items():
            print('node sources', k, len(node_sources))
            for pair in pairwise(node_sources):
                new = Node()
                for node, who in pair:
                    if who is node.winner:
                        new.feeders.append(node)
                        node.winner_child = new
                    elif who is node.loser:
                        new.inverted_feeders.append(node)
                        node.loser_child = new
                    else:
                        logging.error('bad who')
                nodes.append(new)
                pending_matches = True
        if pending_matches:
            break
    return False

def pairwise(collection):
    return zip(*([iter(collection)] * 2))

def generate_triple_elimination_bracket(submissions):
    seed_layer = generate_initial_pairing(submissions)
    for node in seed_layer:
        node.losses = 0
    layers = [seed_layer]
    left_over = list(seed_layer)
    while True:
        if (len(left_over) == 3
                and left_over[0].losses == 0
                and left_over[1].losses == 1
                and left_over[2].losses == 2):
            break
        layer = list()
        next_left_over = list()
        by_losses = collections.defaultdict(lambda: list())
        for node in left_over:
            by_losses[node.losses].append(node)  # Order matters
        for losses, nodes in by_losses.items():
            for pair in pairwise(reversed(nodes)):  # Order matters
                node = Node()
                node.feeders = pair
                node.losses = losses
                layer.append(node)
                if losses < 2:
                    node = Node()
                    node.inverted_feeders = pair
                    node.losses = losses + 1
                    layer.append(node)
            if len(nodes) % 2:
                next_left_over.append(nodes[0])
        layers.append(layer)
        next_left_over.extend(layer)
        left_over = next_left_over
    # Generate pessimistic winners bracket
    while len(left_over) > 1:
        left_over = sorted(left_over, key=lambda n: n.losses)
        n1, n2 = left_over[-2:]
        left_over = left_over[:-2]
        layers.append([n1, n2])
        difference = n2.losses - n1.losses
        for _ in range(difference):
            tiebreaker = Node()
            tiebreaker.feeders = [n1, n2]
            tiebreaker.losses = n2.losses
            layers.append([tiebreaker])
            n2 = tiebreaker
        tiebreaker2 = Node()
        tiebreaker2.feeders = [n1, n2]
        tiebreaker2.losses = n2.losses
        left_over.append(tiebreaker2)
    layers.append([left_over[0]])
    return layers

def get_node_label(node):
    names = list()
    for submission in node.submissions:
        names.append(f'{submission.name}_{submission.id}')
    names += ['-'] * (2 - len(names))
    label = f'{names[0]} vs {names[1]}'
    if node.games:
        left_wins = sum(1 for g in node.games if g.winner_id == node.submissions[0].id)
        right_wins = sum(1 for g in node.games if g.winner_id == node.submissions[1].id)
        label = f'{names[0]}({left_wins}/{BEST_OF}) vs {names[1]}({right_wins}/{BEST_OF})'
        if node.winner:
            representative_games = [g for g in node.games if g.winner_id == node.winner.id]
            if representative_games:
                label += r'\n' + representative_games[0].log_url
    return label

def _print_tree(node, depth):
    global _printed
    if node is None or node in _printed:
        return
    _printed.add(node)
    feeders = list(node.feeders) + list(node.inverted_feeders)
    if len(feeders) >= 1:
        _print_tree(feeders[0], depth - 1)
    line = ' '*10*depth + get_node_label(node)
    print(line)
    if len(feeders) >= 2:
        _print_tree(feeders[1], depth - 1)

def print_tree(levels):
    global _printed
    _printed = set()
    print('-'*40)
    _print_tree(levels[-1][0], len(levels) - 1)
    print('-'*40)

def _dot_tree(node):
    global _printed
    if node is None:
        return
    if node in _printed:
        return
    _printed.add(node)
    for feeder in node.feeders:
        print(f'  {id(feeder)} -> {id(node)} [style=solid];')
    for feeder in node.inverted_feeders:
        print(f'  {id(feeder)} -> {id(node)} [style=dotted];')
    feeders = list(node.feeders) + list(node.inverted_feeders)
    if feeders:
        _dot_tree(feeders[0])
    label = get_node_label(node)
    print(f'  {id(node)} [label="{label}"];')
    if len(feeders) > 1:
        _dot_tree(feeders[1])

def dot_tree(node):
    global _printed
    _printed = set()
    print('digraph bracket {')
    print('  rankdir=LR')
    _dot_tree(node)
    print('}')

def dot_nodes(nodes):
    print('digraph bracket {')
    print('  rankdir=LR')
    for node in nodes:
        for feeder in node.feeders:
            print(f'  {id(feeder)} -> {id(node)} [style=solid];')
        for feeder in node.inverted_feeders:
            print(f'  {id(feeder)} -> {id(node)} [style=dotted];')
        label = get_node_label(node)
        print(f'  {id(node)} [label="{label}"];')
    print('}')

def get_games(conn, game_ids):
    cur = conn.cursor()
    q = '''
SELECT id, status, winner_id, log_url
FROM games
WHERE id IN %s;
    '''
    cur.execute(q, (tuple(game_ids),))
    conn.commit()
    return cur.fetchall()

def update_game_status(conn, levels):
    game_ids = list()
    for level in levels:
        for node in level:
            for game in node.games:
                if game.status != 'finished':
                    game_ids.append(game.id)
    if not game_ids:
        return
    logging.info(f'Retrieving status for {len(game_ids)} games')
    games = get_games(conn, game_ids)
    games_by_id = {g.id: g for g in games}
    for level in levels:
        for node in level:
            for i, game in enumerate(node.games):
                if game.id in games_by_id:
                    node.games[i] = games_by_id[game.id]

def propogate_winners(node):
    if node.feeders or node.inverted_feeders:
        node.submissions = list()
    for feeder in node.feeders:
        if feeder.winner:
            node.submissions.append(feeder.winner)
    for feeder in node.inverted_feeders:
        if feeder.loser:
            node.submissions.append(feeder.loser)

def declare_and_propogate_winners(node):
    if node is None:
        return
    if node.winner and node.loser:
        return
    for feeder in node.feeders:
        declare_and_propogate_winners(feeder)
    propogate_winners(node)
    # Handle buys
    if len(node.submissions) == 2:
        if node.submissions[0] is BUY and node.submissions[1]:
            node.winner = node.submissions[1]
            node.loser = BUY
        if node.submissions[1] is BUY and node.submissions[0]:
            node.winner = node.submissions[0]
            node.loser = BUY
        if node.submissions[0] is BUY and node.submissions[1] is BUY:
            node.winner = node.loser = BUY
    # Handle playing yourself
    if len(node.submissions) == 2:
        if node.submissions[0] == node.submissions[1]:
            node.winner = node.submissions[0]
            node.loser = node.submissions[1]
    # Declare match winners from games played
    if not node.winner:
        winners = collections.Counter(g.winner_id for g in node.games if g.winner_id)
        for winner_id, wins in winners.items():
            if wins > (BEST_OF // 2) and winner_id is not None:
                for pair in zip(node.submissions, reversed(node.submissions)):
                    if pair[0].id == winner_id:
                        node.winner = pair[0]
                        node.loser = pair[1]
                        break
                else:
                    raise Exception(f'Winner {winner_id} was not a member of this node')

def create_queued_game(conn, left_submission, right_submission):
    cur = conn.cursor()
    q = '''
INSERT INTO games (
 status
) VALUES (
'queued'
) RETURNING id, status, winner_id;
    '''
    cur.execute(q)
    game = cur.fetchone()
    q = '''
INSERT INTO games_submissions (
 game_id,
 submission_id
) VALUES
(%s, %s),
(%s, %s)
    '''
    cur.execute(q, (game.id, left_submission.id, game.id, right_submission.id))
    cur.close()
    conn.commit()
    return game

def create_needed_games(conn, levels):
    for level in levels:
        for node in level:
            if not node.winner and len(node.submissions) == 2:
                if BUY in node.submissions:
                    continue
                finished_or_queued_games = [g for g in node.games if g.status in ['finished', 'queued', 'playing']]
                for i in range(len(finished_or_queued_games), BEST_OF):
                    left, right = node.submissions
                    # Try to mitigate first-turn advantage by switching player order
                    if i % 2:
                        left, right = right, left
                    logging.info(f'Enqueueing match for {left.name}({left.id}) vs. {right.name}({right.id})')
                    game = create_queued_game(conn, left, right)
                    node.games.append(game)

if __name__ == '__main__':
    main()
