"""
Questrade API backend.
"""
from . import config
from ..log import get_logger
from pprint import pformat
import time
from async_generator import asynccontextmanager

# TODO: move to urllib3/requests once supported
import asks
asks.init('trio')

log = get_logger('questrade')

_refresh_token_ep = 'https://login.questrade.com/oauth2/'
_version = 'v1'


class QuestradeError(Exception):
    "Non-200 OK response code"


def resproc(
    resp: asks.response_objects.Response,
    return_json: bool = True
) -> asks.response_objects.Response:
    """Raise error on non-200 OK response.
    """
    data = resp.json()
    log.debug(f"Received json contents:\n{pformat(data)}\n")

    if not resp.status_code == 200:
        raise QuestradeError(resp.body)

    return data if return_json else resp


class API:
    """Questrade API at its finest.
    """
    def __init__(self, session: asks.Session):
        self._sess = session

    async def _request(self, path: str) -> dict:
        resp = await self._sess.get(path=f'/{path}')
        return resproc(resp)

    async def accounts(self):
        return await self._request('accounts')

    async def time(self):
        return await self._request('time')


class Client:
    """API client suitable for use as a long running broker daemon.
    """
    def __init__(self, config: dict):
        sess = self._sess = asks.Session()
        self.api = API(sess)
        self.access_data = config
        self.user_data = {}
        self._conf = None  # possibly set in ``from_config`` factory

    @classmethod
    async def from_config(cls, config):
        client = cls(dict(config['questrade']))
        client._conf = config
        await client.enable_access()
        return client

    async def _new_auth_token(self) -> dict:
        """Request a new api authorization ``refresh_token``.

        Gain api access using either a user provided or existing token.
        See the instructions::

        http://www.questrade.com/api/documentation/getting-started
        http://www.questrade.com/api/documentation/security
        """
        resp = await self._sess.get(
            _refresh_token_ep + 'token',
            params={'grant_type': 'refresh_token',
                    'refresh_token': self.access_data['refresh_token']}
        )
        data = resproc(resp)
        self.access_data.update(data)

        return data

    async def _prep_sess(self) -> None:
        """Fill http session with auth headers and a base url.
        """
        data = self.access_data
        # set access token header for the session
        self._sess.headers.update({
            'Authorization': (f"{data['token_type']} {data['access_token']}")})
        # set base API url (asks shorthand)
        self._sess.base_location = self.access_data['api_server'] + _version

    async def _revoke_auth_token(self) -> None:
        """Revoke api access for the current token.
        """
        token = self.access_data['refresh_token']
        log.debug(f"Revoking token {token}")
        resp = await asks.post(
            _refresh_token_ep + 'revoke',
            headers={'token': token}
        )
        return resp

    async def enable_access(self, force_refresh: bool = False) -> dict:
        """Acquire new ``refresh_token`` and/or ``access_token`` if necessary.

        Only needs to be called if the locally stored ``refresh_token`` has
        expired (normally has a lifetime of 3 days). If ``false is set then
        refresh the access token instead of using the locally cached version.
        """
        access_token = self.access_data.get('access_token')
        expires = float(self.access_data.get('expires_at', 0))
        # expired_by = time.time() - float(self.ttl or 0)
        # if not access_token or (self.ttl is None) or (expires < time.time()):
        if not access_token or (expires < time.time()) or force_refresh:
            log.info(
                f"Access token {access_token} expired @ {expires}, "
                "refreshing...")
            data = await self._new_auth_token()

            # store absolute token expiry time
            self.access_data['expires_at'] = time.time() + float(
                data['expires_in'])

        await self._prep_sess()
        return self.access_data


def get_config() -> "configparser.ConfigParser":
    conf, path = config.load()
    if not conf.has_section('questrade') or (
        not conf['questrade'].get('refresh_token')
    ):
        log.warn(
            f"No valid `questrade` refresh token could be found in {path}")
        # get from user
        refresh_token = input("Please provide your Questrade access token: ")
        conf['questrade'] = {'refresh_token': refresh_token}

    return conf


@asynccontextmanager
async def get_client(refresh_token: str = None) -> Client:
    """Spawn a broker client.

    """
    conf = get_config()
    log.debug(f"Loaded questrade config: {conf['questrade']}")
    log.info("Waiting on api access...")
    client = await Client.from_config(conf)

    try:
        try:  # do a test ping to ensure the access token works
            log.debug("Check time to ensure access token is valid")
            await client.api.time()
        except Exception as err:
            # access token is likely no good
            log.warn(f"Access token {client.access_data['access_token']} seems"
                     f" expired, forcing refresh")
            await client.enable_access(force_refresh=True)
            await client.api.time()

        yield client
    finally:
        # save access creds for next run
        conf['questrade'] = client.access_data
        config.write(conf)


async def serve_forever(refresh_token: str = None) -> None:
    """Start up a client and serve until terminated.
    """
    async with get_client(refresh_token) as client:
        # pretty sure this doesn't work
        # await client._revoke_auth_token()
        return client