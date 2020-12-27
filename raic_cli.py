#!/usr/bin/env python3

import os
import getpass
import logging
import random
import glob
import re
from collections import defaultdict
from functools import partial
from copy import deepcopy
from datetime import datetime, timedelta
from time import sleep
from pprint import pprint  # noqa: F401
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

import coloredlogs
import fire
import requests
import yaml
import tqdm
from dateutil import parser
from lxml.html import fromstring
from prettytable import PrettyTable

from fire_utils import only_allow_defined_args


logger = logging.getLogger(__name__)


class SignInFailed(Exception):
    pass


class CreateGameFailed(Exception):
    pass


class ResponseError(Exception):
    pass


class InlineLogger():

    def __init__(self):
        self.last_len = 0

    def __call__(self, msg):
        print('\r' + ' ' * self.last_len, end='')
        print(f'\r{msg}', end='')
        self.last_len = len(msg)

    def clear(self):
        if self.last_len:
            self("")

    def __del__(self):
        self.clear()


inline_logger = InlineLogger()


def wait(value):
    logger.debug(f'Wait {value}')

    if isinstance(value, datetime):
        finish_time = value
    elif isinstance(value, timedelta):
        finish_time = datetime.now() + value
    else:
        finish_time = datetime.now() + timedelta(seconds=value)

    while True:
        now = datetime.now()
        if now > finish_time:
            break
        delta = finish_time - now
        minutes, seconds = divmod(int(delta.total_seconds()), 60)
        inline_logger(f'waiting... {minutes}:{seconds:02d}')
        sleep(1)
    inline_logger.clear()
    logger.debug('Wait done')


def ensure_folder(folder):
    os.makedirs(folder, exist_ok=True)


def pretty_table_from_dict(data):
    headers = data['headers']
    table = PrettyTable(headers)
    for k, v in data.get('alignment', {}).items():
        table.align[k] = v

    sorting = data.get('sort')
    if sorting:
        table.sortby = sorting['by']
        table.reversesort = sorting.get('reverse', False)
    return table


class UserFolder:

    def __init__(self, username, cache_folder):
        self.username = username
        self.folder = os.path.join(cache_folder, username)
        self.games_folder = os.path.join(self.folder, 'games')
        ensure_folder(self.games_folder)

    @property
    def data_file(self):
        return os.path.join(self.folder, 'data.yaml')

    def read_data(self):
        data_file = self.data_file
        if os.path.exists(data_file):
            with open(data_file, 'r') as fo:
                data = yaml.safe_load(fo)
        else:
            data = {}
        return data

    def user_id(self):
        return self.read_data().get('user_id')

    def write_data(self, data):
        with open(self.data_file, 'w') as fo:
            yaml.dump(data, fo, indent=2)

    def game_file(self, game_id):
        game_id = f'{game_id:>08s}'
        filepath = os.path.join(self.games_folder, game_id[:4], f'{game_id}.yaml')
        ensure_folder(os.path.dirname(filepath))
        return filepath

    def exists_game(self, game_id):
        return os.path.exists(self.game_file(game_id))

    def read_game(self, game_file):
        with open(game_file, 'r') as fo:
            game_data = yaml.safe_load(fo)

        info = game_data['game']
        ret = {
            'info': info,
            'participants': [],
            'users': {},
        }
        rating_changes = game_data.get('ratingChanges')

        users = game_data['usersRaw'] or game_data['users']
        ret['users'] = {u['login'] for u in users}

        for idx, participant in enumerate(game_data['gameParticipants']):
            if rating_changes:
                participant['ratingChanges'] = rating_changes[idx]

            for line in participant['strategyProtocol'].split('\n')[::-1]:
                line = line.strip()
                if line.startswith('Consumed time'):
                    participant['time'] = line.split(':')[-1].strip()
                elif line.startswith('Memory used'):
                    participant['memory'] = line.split(':')[-1].strip()
                    break

            ret['participants'].append(participant)

        info['creation_time'] = parser.parse(info['creationTime'])
        return ret

    def write_game(self, game_id, data):
        with open(self.game_file(game_id), 'w') as fo:
            yaml.dump(data, fo, indent=2)

    def games(self):
        files = glob.glob(os.path.join(self.games_folder, '**/*.yaml'))
        files.sort(reverse=True)

        with ProcessPoolExecutor() as executor, tqdm.tqdm(total=len(files), leave=True) as pbar:
            for game in executor.map(partial(self.read_game), files):
                pbar.update()
                yield game


class RAIC:

    def __init__(self, cookie_file, cache_folder, host='https://russianaicup.ru/'):
        self.host = host
        self.cookie_file = cookie_file
        self.cache_folder = cache_folder
        self.session = requests.session()
        self.load_cookies()
        self.cache = {}
        self.inline_log = InlineLogger()

    def __del__(self):
        self.save_cookies()

    def load_cookies(self):
        if os.path.exists(self.cookie_file):
            with open(self.cookie_file, 'r') as fo:
                self.session.cookies.update(yaml.full_load(fo))
        logger.debug('Cookies loaded')

    def save_cookies(self):
        with open(self.cookie_file, 'w') as fo:
            yaml.dump(self.session.cookies, fo)
        logger.debug('Cookies saved')

    def get(self, url, method='get', parse=False, **kwargs):
        logger.debug(f'{method} {url}')
        func = getattr(self.session, method)
        kwargs.setdefault('timeout', 60)

        n_attempt = 5
        while True:
            try:
                inline_logger(f'{method} {url}')
                response = func(urljoin(self.host, url), **kwargs)
                if response.status_code != 200:
                    if n_attempt == 0:
                        raise ResponseError(response)
                    n_attempt -= 1
                    wait(5)
                    continue
                else:
                    break
            except Exception as e:
                inline_logger.clear()
                logger.error(e)
                wait(60)
        inline_logger.clear()

        if 'application/json' in response.headers.get('content-type'):
            return response.json()
        if parse:
            response = fromstring(response.content)
            token = response.xpath('//meta[@name="X-Csrf-Token"]/@content')
            if token:
                self.csrf_token = token[0]
        return response

    def post(self, *args, **kwargs):
        kwargs['method'] = 'post'
        return self.get(*args, **kwargs)

    @staticmethod
    def total_num_pages(page):
        page_nums = page.xpath('//*[@class="page-index"]/a/text()')
        return int(page_nums[-1]) if page_nums else None

    @staticmethod
    def user_id(page):
        match = re.search(r'userId\s*:\s*([0-9]+)', page)
        return int(match.group(1)) if match else None

    def is_authorized(self, page):
        return bool(page.xpath('//a[@class="logout" and contains(@href, "signOut")]'))

    def signin(self):
        page = self.get('/signIn', parse=True)
        if self.is_authorized(page):
            return

        username = input('username or email: ')
        password = getpass.getpass('password (is not stored anywhere): ')

        form = page.forms[-1]
        form.fields['loginOrEmail'] = username
        form.fields['password'] = password

        page = self.post('/signIn', data=dict(form.fields), parse=True)
        if self.has_errors(page):
            raise SignInFailed()
        assert self.is_authorized(page), 'Sign in failed'

    def has_errors(self, page):
        errors = page.xpath('//*[contains(@class, "error")]//*[contains(@class, "help-block")]/text()')
        if errors:
            for error in errors:
                logger.error(f'{error}')
            return errors
        return False

    def suggest(self, username):
        users = self.cache.get(username)
        if not users:
            data = self.post('/data/suggestUser', data={
                'action': 'getRandomUsers',
                'otherUserLogin': username,
                'csrf_token': self.csrf_token,
            })
            users = data['randomUsers'].split('|')
            users = [{'username': user} for user in users]
            self.cache[username] = users
        return users

    def top(self, sources):
        ret = []
        for source in sources:
            contest = source['contest']
            number = source['number']
            without = source.get('without')
            key = (contest, number, without)
            users = self.cache.get(key)
            if not users:
                contest = self.contest_id(contest)
                if without:
                    without = self.contest_id(without)

                users = []
                page_num = 1
                total_num_pages = None
                while len(users) < number and (total_num_pages is None or page_num <= total_num_pages):
                    url = f'/contest/{contest}/standings'
                    if without:
                        url = f'{url}/without/{without}'
                    page = self.get(f'{url}/page/{page_num}', parse=True)

                    members = page.xpath('//tr[contains(@id, "standings-row-for-place")]//a[contains(@href, "/profile/")]/img[@title]/@title')  # noqa
                    users.extend(members)

                    if total_num_pages is None:
                        total_num_pages = self.total_num_pages(page)
                        if total_num_pages is None:
                            break

                    inline_logger(f'top... {page_num} of {total_num_pages}')
                    page_num += 1
                inline_logger.clear()

                users = users[:number]
                users = [{'username': user} for user in users]
                self.cache[key] = users
            ret.extend(users)
        return ret

    def clear_cache(self):
        self.cache = {}

    def create_game(self, users, formats, allow_duplicate_users):
        game_params = {
            'action': 'createGame',
            'csrf_token': self.csrf_token,
            'gameFormat': random.choice(formats),
        }
        self.clear_cache()
        username_for_suggest = None
        strategies = []
        users = deepcopy(users)
        used = set()
        for participant_idx, user in enumerate(users, start=1):
            query = user.pop('query', None)
            if 'username' not in user:
                if query == 'suggest':
                    assert username_for_suggest, 'Suggest query must be after user with username set'
                    users = self.suggest(username_for_suggest)
                elif query == 'top':
                    users = self.top(user.pop('sources'))
                elif query == 'random':
                    users = user.pop('users')
                else:
                    raise ValueError(f'Unknown query "{query}"')

                logger.debug(f'Random choice from {len(users)} users')
                while True:
                    idx = random.randint(1, len(users)) - 1
                    user = users.pop(idx)
                    if allow_duplicate_users or user['username'] not in used:
                        break
                    logger.debug(f'Skip {user}')
            else:
                username_for_suggest = user['username']
            username = user['username']
            used.add(username)

            strategy = user.get('strategy')
            if not strategy:
                data = self.post('/data/suggestUser', data={
                    'action': 'findStrategyVersions',
                    'userLogin': username,
                    'csrf_token': self.csrf_token,
                })
                strategy = int(data['strategyCount'])

            game_params[f'participant{participant_idx}'] = username
            game_params[f'participant{participant_idx}Strategy'] = strategy - 1
            strategies.append(f'{username}#{strategy}')
            logger.debug(f'Pick {username}#{strategy}')

        logger.info(' vs '.join(strategies))

        page = self.post('/game/create', data=game_params, parse=True)
        errors = self.has_errors(page)
        if errors:
            raise CreateGameFailed(errors)

    def fetch_games(self, username):
        user = UserFolder(username, self.cache_folder)
        user_data = user.read_data()

        game_ids = []
        page_num = 1
        total_num_pages = None
        while total_num_pages is None or page_num <= total_num_pages:
            page = self.get(f'/profile/{username}/allGames/page/{page_num}', parse=True)

            ids = [str(i) for i in page.xpath('//a[starts-with(@href, "/game/view/") and not(@style)]/text()')]
            game_ids.extend(ids)

            if total_num_pages is None:
                total_num_pages = self.total_num_pages(page)
                if total_num_pages is None:
                    break
            if user_data.get("last_game_id") in ids:
                break
            of = total_num_pages - user_data.get("total_num_pages", 1) + 1
            inline_logger(f'fetch game pages... {page_num} of {of}')
            page_num += 1
        inline_logger.clear()

        if not game_ids:
            return

        def fetch_and_save_game_data(game_id):
            if user.exists_game(game_id):
                return
            data = self.post('/data/gameInformation', data={
                'gameId': game_id,
                'csrf_token': self.csrf_token,
            })
            user.write_game(game_id, data)

        with ThreadPoolExecutor() as executor, tqdm.tqdm(total=len(game_ids), leave=False) as pbar:
            for _ in executor.map(fetch_and_save_game_data, game_ids):
                pbar.update()

        user_data['last_game_id'] = game_ids[0]
        user_data['total_num_pages'] = total_num_pages

        user.write_data(user_data)

    def games(self, username):
        user = UserFolder(username, self.cache_folder)
        for game in user.games():
            users_by_id = {}
            for username in game['users']:
                user_folder = UserFolder(username, self.cache_folder)
                user_id = user_folder.user_id()
                if user_id is None:
                    response = self.get(f'/profile/{username}')
                    user_id = self.user_id(response.content.decode('utf8'))
                    assert user_id, 'User id must be got'
                    user_data = user_folder.read_data()
                    user_data['user_id'] = user_id
                    user_folder.write_data(user_data)
                users_by_id[user_id] = username

            participants = {}
            for p in game['participants']:
                username = users_by_id[p['userId']]
                p['username'] = username
                participants[username] = p
            game['participants'] = participants
            yield game

    def game_url(self, game_id):
        return urljoin(self.host, f'/game/view/{game_id}')

    def contest_id(self, name):
        return {
            'sandbox': 1,
            'round1': 2,
            'round2': 3,
            'finals': 4,
        }[name]


class Main:

    def __init__(
        self,
        config_file=os.path.join(os.path.dirname(__file__), 'config.yaml'),
        cookie_file=os.path.join(os.path.dirname(__file__), 'cookies.yaml'),
        cache_folder=os.path.join(os.path.dirname(__file__), 'cache'),
        verbose=False,
    ):
        level = logging.DEBUG if verbose else logging.INFO
        coloredlogs.install(level=level, fmt='%(asctime)s %(levelname)s %(message)s', logger=logger)

        with open(config_file, 'r') as fo:
            self._config = yaml.safe_load(fo)
        self._raic = RAIC(cookie_file=cookie_file, cache_folder=cache_folder)
        self._raic.signin()

    @only_allow_defined_args
    def create_game(self, limit=1, limit_game=None, limit_delay=None, allow_duplicate_users=False):
        timing = []
        create_game = self._config['create-game']
        while True:
            if limit_game:
                while len(timing) > limit_game:
                    timing.pop(0)
                if len(timing) == limit_game:
                    wait(timing.pop(0) + timedelta(minutes=limit_delay))

            while True:
                try:
                    self._raic.create_game(create_game['users'], create_game['formats'], allow_duplicate_users)
                    timing.append(datetime.now())
                    break
                except CreateGameFailed as e:
                    for error in e.args[0]:
                        match = re.search('You can not create more than ([0-9]+) games in ([0-9]+) minutes', error)
                        if match:
                            limit_game = int(match.group(1))
                            limit_delay = int(match.group(2))
                    if limit_delay is not None and limit_game is not None:
                        delay_on_failed = limit_delay / limit_game
                    else:
                        delay_on_failed = 60
                    wait(timedelta(minutes=delay_on_failed))
                    continue

            if limit:
                limit -= 1
                if not limit:
                    break

    def find_games(self, username, limit=10, **kwargs):
        self._raic.fetch_games(username)

        find_games = deepcopy(self._config['find-games'])
        find_games.update(kwargs)

        users = find_games.get('users')
        if users:
            if isinstance(users, str):
                users = [users]
            users = set(users)

        contest = find_games.get('contest')
        if contest:
            contest_id = self._raic.contest_id(contest)

        datetime_from = find_games.get('datetime_from')
        if datetime_from:
            datetime_from = parser.parse(datetime_from)

        games = []

        for game in self._raic.games(username):
            info = game['info']
            user_info = game['participants'][username]

            if datetime_from and datetime_from > info['creation_time']:
                break

            if find_games.get('attributes') and find_games['attributes'] != info['attributes']:
                continue

            if find_games.get('rank') and find_games['rank'] != user_info['rank']:
                continue

            if find_games.get('strategy') and find_games['strategy'] != user_info['strategyVersion']:
                continue

            if users and not users & game['users']:
                continue

            if contest and contest_id != info.get('contestId'):
                continue

            games.append(game)

            if limit:
                limit -= 1
                if not limit:
                    break

        table = pretty_table_from_dict(find_games)
        sortby = getattr(table, 'sortby', None)

        statistics = {}
        games_num_rows = []
        for game in games:
            url = self._raic.game_url(game['info']['id'])
            num_rows = 0
            if not games_num_rows:
                num_rows += 3
            ctime = game['info']['creationTime']
            my_rank = game['participants'][username]['rank']
            for p in sorted(game['participants'].values(), key=lambda p: p['score'], reverse=True):
                p_user = p['username']
                p['url'] = url
                p['ctime'] = ctime
                if not sortby:
                    url = ''
                    ctime = ''
                p['strategy'] = f"{'* ' if username == p_user else ''}{p_user}#{p['strategyVersion']}"
                table.add_row([p.get(k, '') for k in table.field_names])
                num_rows += 1

                if p_user != username:
                    stat = statistics.setdefault(p_user, defaultdict(int))
                    stat['total'] += 1
                    stat['n_win'] += my_rank < p['rank']
                    stat['n_lose'] += my_rank > p['rank']
            games_num_rows.append(num_rows)

        return_data = find_games.get('return_data')

        if not return_data:
            if sortby:
                print(table)
            else:
                lines = table.get_string().splitlines()
                sep = lines.pop(-1)
                idx = 0
                for line in lines:
                    print(line)
                    games_num_rows[idx] -= 1
                    if games_num_rows[idx] == 0:
                        print(sep)
                        idx += 1

        stats_info = find_games.get('statistics')
        if stats_info:
            stat_table = pretty_table_from_dict(stats_info)
            total = defaultdict(int)
            total['user'] = 'TOTAL:'

            def update_stat(stat):
                stat['win'] = f"{stat['n_win'] / stat['total']:.3f}"
                stat['lose'] = f"{stat['n_lose'] / stat['total']:.3f}"

            for stat in statistics.values():
                update_stat(stat)
            for user, stat in sorted(statistics.items(), key=lambda v: v[1]['win']):
                stat['user'] = user
                stat_table.add_row([stat.get(k, '') for k in stat_table.field_names])
                for k in 'total', 'n_win', 'n_lose':
                    total[k] += stat[k]
                total['win'] += float(stat['win'])
            total['win'] = f"{total['win'] / len(statistics):.3f}"
            stat_table.add_row([total.get(k, '') for k in stat_table.field_names])
            if not return_data:
                print(stat_table)

        if return_data:
            ret = {}
            if stats_info:
                ret['total'] = total
            return ret

    def win_rates(self, **kwargs):
        win_rates = deepcopy(self._config['win-rates'])
        users = self._raic.top(win_rates['sources'])
        table = pretty_table_from_dict(win_rates)
        for user in users:
            username = user['username']
            data = self.find_games(username, limit=False, return_data=True, **kwargs)
            values = data.get('total', {})
            values['user'] = username
            table.add_row([values.get(k, '') for k in table.field_names])
        print(table)


if __name__ == '__main__':
    fire.Fire(Main)
