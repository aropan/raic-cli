#!/usr/bin/env python3

import os
import getpass
import logging
import random
from datetime import datetime, timedelta
from time import sleep
from urllib.parse import urljoin

import coloredlogs
import fire
import requests
import yaml
from lxml.html import fromstring


logger = logging.getLogger(__name__)


class SignInFailed(Exception):
    pass


class CreateGameFailed(Exception):
    pass


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
        print(f'\rwaiting... {minutes}:{seconds:02d}', end='')
        sleep(1)
    print(end='\r')
    logger.debug('Wait done')


class RAIC:

    def __init__(self, cookie_file, host='https://russianaicup.ru/'):
        self.host = host
        self.cookie_file = cookie_file
        self.session = requests.session()
        self.load_cookies()
        self.cache = {}

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
        response = func(urljoin(self.host, url), **kwargs)
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

    def get_suggest(self, username):
        users = self.cache.get(username)
        if not users:
            data = self.post('/data/suggestUser', data={
                'action': 'getRandomUsers',
                'otherUserLogin': username,
                'csrf_token': self.csrf_token,
            })
            users = data['randomUsers'].split('|')
            random.shuffle(users)
            self.cache[username] = users
        return users.pop(0)

    def clear_cache(self):
        self.cache = {}

    def create_game(self, users, formats):
        game_params = {
            'action': 'createGame',
            'csrf_token': self.csrf_token,
            'gameFormat': random.choice(formats),
        }
        self.clear_cache()
        username_for_suggest = None
        strategies = []
        for idx, user in enumerate(users, start=1):
            query = user.get('query')
            if query == 'suggest':
                assert username_for_suggest, 'Suggest query must be after user with username set'
                username = self.get_suggest(username_for_suggest)
            else:
                username = user['username']
                username_for_suggest = username

            strategy = user.get('strategy')
            if not strategy:
                data = self.post('/data/suggestUser', data={
                    'action': 'findStrategyVersions',
                    'userLogin': username,
                    'csrf_token': self.csrf_token,
                })
                strategy = int(data['strategyCount'])

            game_params[f'participant{idx}'] = username
            game_params[f'participant{idx}Strategy'] = strategy - 1
            strategies.append(f'{username}#{strategy}')

        logger.info(' vs '.join(strategies))

        page = self.post('/game/create', data=game_params, parse=True)
        if self.has_errors(page):
            raise CreateGameFailed()


class Main:

    def __init__(
        self,
        config_file=os.path.join(os.path.dirname(__file__), 'config.yaml'),
        cookie_file=os.path.join(os.path.dirname(__file__), 'cookies.yaml'),
        verbose=False,
    ):
        level = logging.DEBUG if verbose else logging.INFO
        coloredlogs.install(level=level, fmt='%(asctime)s %(levelname)s %(message)s', logger=logger)

        with open(config_file, 'r') as fo:
            self._config = yaml.safe_load(fo)
        self._raic = RAIC(cookie_file)
        self._raic.signin()

    def create_game(self, limit=1, delay_on_failed=5, limit_game=4, limit_delay=20):
        timing = []
        while True:
            if len(timing) == limit_game:
                wait(timing.pop(0) + timedelta(minutes=limit_delay))

            while True:
                try:
                    self._raic.create_game(self._config['users'], self._config['formats'])
                    timing.append(datetime.now())
                    break
                except CreateGameFailed:
                    wait(timedelta(minutes=delay_on_failed))
                    continue

            if limit:
                limit -= 1
                if not limit:
                    break


if __name__ == '__main__':
    fire.Fire(Main)
