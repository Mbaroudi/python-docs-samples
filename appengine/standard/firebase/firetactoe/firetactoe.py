# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tic Tac Toe with the Firebase API"""

import datetime
import json
import os
import re
import urllib

from Crypto.PublicKey import RSA
import flask
from flask import request
from google.appengine.api import users
from google.appengine.ext import ndb
import httplib2
import jwt
from oauth2client.service_account import ServiceAccountCredentials


_FIREBASE_CONFIG = '_firebase_config.html'
_SERVICE_ACCOUNT_FILENAME = 'credentials.json'

_CWD = os.path.dirname(__file__)
_IDENTITY_ENDPOINT = ('https://identitytoolkit.googleapis.com/'
                      'google.identity.identitytoolkit.v1.IdentityToolkit')
_FIREBASE_SCOPES = [
    'https://www.googleapis.com/auth/firebase.database',
    'https://www.googleapis.com/auth/userinfo.email']


app = flask.Flask(__name__)


def _get_firebase_db_url(_memo={}):
    """Grabs the databaseURL from the Firebase config snippet."""
    # Memoize the value, to avoid parsing the code snippet every time
    if 'dburl' not in _memo:
        regex = re.compile(r'\bdatabaseURL\b.*?["\']([^"\']+)')
        cwd = os.path.dirname(__file__)
        with open(os.path.join(cwd, 'templates', _FIREBASE_CONFIG)) as f:
            url = next(regex.search(line) for line in f if regex.search(line))
        _memo['dburl'] = url.group(1)
    return _memo['dburl']


def _get_http(_memo={}):
    """Provides an authed http object."""
    if 'http' not in _memo:
        # Memoize the authorized http object, to avoid fetching new access tokens
        http = httplib2.Http()
        # Use service account credentials to make the Firebase calls
        # https://firebase.google.com/docs/reference/rest/database/user-auth
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            os.path.join(_CWD, _SERVICE_ACCOUNT_FILENAME), _FIREBASE_SCOPES)
        creds.authorize(http)
        _memo['http'] = http
    return _memo['http']


def _send_firebase_message(u_id, message=None):
    url = '{}/channels/{}.json'.format(_get_firebase_db_url(), u_id)

    if message:
        return _get_http().request(url, 'PATCH', body=message)
    else:
        return _get_http().request(url, 'DELETE')


def create_custom_token(uid):
    """Create a secure token for the given id.

    This method is used to create secure custom tokens to be passed to clients
    it takes a unique id (uid) that will be used by Firebase's security rules
    to prevent unauthorized access. In this case, the uid will be the channel
    id which is a combination of user_id and game_key
    """
    with open(os.path.join(_CWD, _SERVICE_ACCOUNT_FILENAME), 'r') as f:
        credentials = json.load(f)

    payload = {
        'iss': credentials['client_email'],
        'sub': credentials['client_email'],
        'aud': _IDENTITY_ENDPOINT,
        'uid': uid,
    }
    exp = datetime.timedelta(minutes=60)
    return jwt.generate_jwt(
        payload, RSA.importKey(credentials['private_key']), 'RS256', exp)


class Wins():
    """A collection of patterns of winning boards."""
    x_win_patterns = ['XXX......',
                      '...XXX...',
                      '......XXX',
                      'X..X..X..',
                      '.X..X..X.',
                      '..X..X..X',
                      'X...X...X',
                      '..X.X.X..']
    o_win_patterns = map(lambda s: s.replace('X', 'O'), x_win_patterns)

    x_wins = map(lambda s: re.compile(s), x_win_patterns)
    o_wins = map(lambda s: re.compile(s), o_win_patterns)


class Game(ndb.Model):
    """All the data we store for a game"""
    userX = ndb.UserProperty()
    userO = ndb.UserProperty()
    board = ndb.StringProperty()
    moveX = ndb.BooleanProperty()
    winner = ndb.StringProperty()
    winning_board = ndb.StringProperty()

    def to_json(self):
        d = self.to_dict()
        d['winningBoard'] = d.pop('winning_board')
        return json.dumps(d, default=lambda user: user.user_id())

    def send_update(self):
        """Updates Firebase's copy of the board."""
        message = self.to_json()
        # send updated game state to user X
        _send_firebase_message(
            self.userX.user_id() + self.key.id(),
            message=message)
        # send updated game state to user O
        if self.userO:
            _send_firebase_message(
                self.userO.user_id() + self.key.id(),
                message=message)

    def _check_win(self):
        if self.moveX:
            # O just moved, check for O wins
            wins = Wins.o_wins
            potential_winner = self.userO.user_id()
        else:
            # X just moved, check for X wins
            wins = Wins.x_wins
            potential_winner = self.userX.user_id()

        for win in wins:
            if win.match(self.board):
                self.winner = potential_winner
                self.winning_board = win.pattern
                return

        # In case of a draw, everyone loses.
        if ' ' not in self.board:
            self.winner = 'Noone'

    def make_move(self, position, user):
        # If the user is a player, and it's their move
        if (user in (self.userX, self.userO)) and (
                self.moveX == (user == self.userX)):
            boardList = list(self.board)
            # If the spot you want to move to is blank
            if (boardList[position] == ' '):
                boardList[position] = 'X' if self.moveX else 'O'
                self.board = ''.join(boardList)
                self.moveX = not self.moveX
                self._check_win()
                self.put()
                self.send_update()
                return


@app.route('/move', methods=['POST'])
def move():
    game = Game.get_by_id(request.args.get('g'))
    position = int(request.args.get('i'))
    if not (game and (0 <= position <= 8)):
        return 'Game not found, or invalid position', 400
    game.make_move(position, users.get_current_user())
    return ''


@app.route('/delete', methods=['POST'])
def delete():
    game = Game.get_by_id(request.args.get('g'))
    if not game:
        return 'Game not found', 400
    user = users.get_current_user()
    _send_firebase_message(
        user.user_id() + game.key.id(), message=None)
    return ''


@app.route('/opened', methods=['POST'])
def opened():
    game = Game.get_by_id(request.args.get('g'))
    if not game:
        return 'Game not found', 400
    game.send_update()
    return ''


@app.route('/')
def main_page():
    """Renders the main page. When this page is shown, we create a new
    channel to push asynchronous updates to the client."""
    user = users.get_current_user()
    game_key = request.args.get('g')

    if not game_key:
        game_key = user.user_id()
        game = Game(id=game_key, userX=user, moveX=True, board=' '*9)
        game.put()
    else:
        game = Game.get_by_id(game_key)
        if not game:
            return 'No such game', 404
        if not game.userO:
            game.userO = user
            game.put()

    # choose a unique identifier for channel_id
    channel_id = user.user_id() + game_key
    # encrypt the channel_id and send it as a custom token to the
    # client
    # Firebase's data security rules will be able to decrypt the
    # token and prevent unauthorized access
    client_auth_token = create_custom_token(channel_id)
    _send_firebase_message(
        channel_id, message=game.to_json())

    game_link = '{}?g={}'.format(request.base_url, game_key)

    # push all the data to the html template so the client will
    # have access
    template_values = {
        'token': client_auth_token,
        'channel_id': channel_id,
        'me': user.user_id(),
        'game_key': game_key,
        'game_link': game_link,
        'initial_message': urllib.unquote(game.to_json())
    }

    return flask.render_template('fire_index.html', **template_values)
