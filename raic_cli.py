#!/usr/bin/env python3

import os
import getpass
import logging
import random
import glob
from copy import deepcopy
from datetime import datetime, timedelta
from time import sleep
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor


import coloredlogs
import fire
import requests
import yaml
import tqdm
from lxml.html import fromstring


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


class UserFolder:

    def __init__(self, username, cache_folder):
        self.username = username
        self.folder = os.path.join(cache_folder, username)
        self.games_folder = os.path.join(self.folder, 'games')

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

        ret = {
            'info': game_data['game'],
            'participants': {},
        }
        rating_changes = game_data.get('ratingChanges')
        for idx, (participant, user) in enumerate(zip(game_data['gameParticipants'], game_data['users'])):
            user = user['login']
            participant['user'] = user
            if rating_changes:
                participant['ratingChanges'] = rating_changes[idx]
            ret['participants'][user] = participant

        return ret

    def write_game(self, game_id, data):
        with open(self.game_file(game_id), 'w') as fo:
            yaml.dump(data, fo, indent=2)

    def games(self):
        files = glob.glob(os.path.join(self.games_folder, '**/*.yaml'))
        files.sort(reverse=True)

        with ProcessPoolExecutor() as executor, tqdm.tqdm(total=len(files), leave=True) as pbar:
            for game in executor.map(self.read_game, files):
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
            response = response.json()
        elif parse:
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
            return True
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
        if self.has_errors(page):
            raise CreateGameFailed()

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
        return user.games()

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

    def create_game(self, limit=1, delay_on_failed=5, limit_game=4, limit_delay=20, allow_duplicate_users=False):
        timing = []
        while True:
            if len(timing) == limit_game:
                wait(timing.pop(0) + timedelta(minutes=limit_delay))

            while True:
                try:
                    self._raic.create_game(self._config['users'], self._config['formats'], allow_duplicate_users)
                    timing.append(datetime.now())
                    break
                except CreateGameFailed:
                    wait(timedelta(minutes=delay_on_failed))
                    continue

            if limit:
                limit -= 1
                if not limit:
                    break

    def find_games(self, username, limit=10, contest=None, rank=None):
        self._raic.fetch_games(username)

        if contest:
            contest = self._raic.contest_id(contest)

        games = []
        for game in self._raic.games(username):
            info = game['info']
            if contest and info.get('contestId') != contest:
                continue

            participant = game['participants'][username]
            if rank and participant.get('rank') != rank:
                continue

            games.append(game)

            if limit:
                limit -= 1
                if not limit:
                    break

        for game in games:
            url = self._raic.game_url(game['info']['id'])
            print(url)


if __name__ == '__main__':
    fire.Fire(Main)
