"""ADFSAuth.

Description: Backend Authentication/Authorization using Okta Service.
"""
import base64
from aiohttp import web
import jwt

# needed by ADFS
import requests
import requests.adapters
from navconfig.logging import logging
from navigator_auth.exceptions import AuthException
from navigator_auth.conf import (
    ADFS_SERVER,
    ADFS_CLIENT_ID,
    ADFS_TENANT_ID,
    ADFS_RESOURCE,
    ADFS_DEFAULT_RESOURCE,
    ADFS_AUDIENCE,
    ADFS_SCOPES,
    ADFS_ISSUER,
    USERNAME_CLAIM,
    GROUP_CLAIM,
    ADFS_CLAIM_MAPPING,
    ADFS_CALLBACK_REDIRECT_URL,
    ADFS_LOGIN_REDIRECT_URL,
    AZURE_AD_SERVER,
    exclude_list
)
from .jwksutils import get_public_key
from .external import ExternalAuth

_jwks_cache = {}


class ADFSAuth(ExternalAuth):
    """ADFSAuth.

    Description: Authentication Backend using
    Active Directory Federation Service (ADFS).
    """

    _service_name: str = "adfs"
    user_attribute: str = "user"
    userid_attribute: str = "upn"
    username_attribute: str = "upn"
    pwd_atrribute: str = "password"
    version = "v1.0"
    user_mapping: dict = {
        "user_id": "upn",
        "email": "email",
        "given_name": "given_name",
        "family_name": "family_name",
        "groups": "group",
        "department": "Department",
        "name": "Display-Name",
    }

    def configure(self, app):
        router = app.router
        # URIs:
        if ADFS_TENANT_ID:
            self.server = AZURE_AD_SERVER
            self.tenant_id = ADFS_TENANT_ID
            self.username_claim = "upn"
            self.groups_claim = "groups"
            self.claim_mapping = ADFS_CLAIM_MAPPING
            self.discovery_oid_uri = f"https://login.microsoftonline.com/{self.tenant_id}/.well-known/openid-configuration"
        else:
            self.server = ADFS_SERVER
            self.tenant_id = "adfs"
            self.username_claim = USERNAME_CLAIM
            self.groups_claim = GROUP_CLAIM
            self.claim_mapping = ADFS_CLAIM_MAPPING
            self.discovery_oid_uri = (
                f"https://{self.server}/adfs/.well-known/openid-configuration"
            )
            self._discovery_keys_uri = f"https://{self.server}/adfs/discovery/keys"

        self.base_uri = f"https:://{self.server}/"
        self.end_session_endpoint = (
            f"https://{self.server}/{self.tenant_id}/ls/?wa=wsignout1.0"
        )
        self._issuer = f"https://{self.server}/{self.tenant_id}/services/trust"
        self.authorize_uri = f"https://{self.server}/{self.tenant_id}/oauth2/authorize/"
        self._token_uri = f"https://{self.server}/{self.tenant_id}/oauth2/token"
        self.userinfo_uri = f"https://{self.server}/{self.tenant_id}/userinfo"

        if ADFS_LOGIN_REDIRECT_URL is not None:
            login = ADFS_LOGIN_REDIRECT_URL
        else:
            login = f"/api/v1/auth/{self._service_name}"

        if ADFS_CALLBACK_REDIRECT_URL is not None:
            callback = ADFS_CALLBACK_REDIRECT_URL
            self.redirect_uri = "{domain}" + callback
        else:
            callback = f"/auth/{self._service_name}/callback"
        # Excluding Redirect for Authorization
        exclude_list.append(self.redirect_uri)
        # Login and Redirect Routes:
        router.add_route(
            "GET", login, self.authenticate, name=f"{self._service_name}_login"
        )
        # finish login (callback)
        router.add_route(
            "*",
            callback,
            self.auth_callback,
            name=f"{self._service_name}_callback_login",
        )
        super(ADFSAuth, self).configure(app)

    async def authenticate(self, request: web.Request):
        """Authenticate, refresh or return the user credentials.

        Description: This function returns the ADFS authorization URL.
        """
        domain_url = self.get_domain(request)
        self.redirect_uri = self.redirect_uri.format(
            domain=domain_url, service=self._service_name
        )
        ## getting Finish Redirect URL
        self.get_finish_redirect_url(request)
        try:
            self.state = base64.urlsafe_b64encode(self.redirect_uri.encode()).decode()
            query_params = {
                "client_id": ADFS_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                # "resource": ADFS_RESOURCE,
                "resource": ADFS_DEFAULT_RESOURCE,
                "response_mode": "query",
                "state": self.state,
                "scope": ADFS_SCOPES,
            }
            self.logger.debug(" === AUTH Params === ")
            self.logger.debug(f"{query_params!s}")
            params = requests.compat.urlencode(query_params)
            login_url = f"{self.authorize_uri}?{params}"
            # Step A: redirect
            return self.redirect(login_url)
        except Exception as err:
            self.logger.exception(err)
            raise AuthException(
                f"Client doesn't have info for ADFS Authentication: {err}"
            ) from err

    async def auth_callback(self, request: web.Request):
        domain_url = self.get_domain(request)
        self.redirect_uri = self.redirect_uri.format(
            domain=domain_url, service=self._service_name
        )
        try:
            auth_response = dict(request.rel_url.query.items())
            if 'error' in auth_response:
                self.logger.exception(
                    f"ADFS: Error getting User information: {auth_response!r}"
                )
                raise web.HTTPForbidden(
                    reason=f"ADFS: Unable to Authenticate: {auth_response!r}"
                )
            authorization_code = auth_response["code"]
            # state = auth_response[
            #     "state"
            # ]  # TODO: making validation with previous state
            # request_id = auth_response["client-request-id"]
        except Exception as err:
            raise web.HTTPForbidden(
                reason=f"ADFS: Invalid Callback response: {err}: {auth_response}"
            ) from err
        # print(authorization_code, state, request_id)
        self.logger.debug(
            f"Received Authorization Code: {authorization_code}"
        )
        # getting an Access Token
        query_params = {
            "code": authorization_code,
            "client_id": ADFS_CLIENT_ID,
            "grant_type": "authorization_code",
            "redirect_uri": 'https://api.dev.navigator.mobileinsight.com/auth/adfs/callback',
            "scope": ADFS_SCOPES,
        }
        self.logger.debug(
            f'Token Params: {query_params!r}'
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            exchange = await self.post(
                self._token_uri, data=query_params, headers=headers
            )
            if "error" in exchange:
                error = exchange.get("error")
                desc = exchange.get("error_description")
                message = f"ADFS {error}: {desc}¡"
                self.logger.exception(message)
                raise web.HTTPForbidden(reason=message)
            else:
                ## processing the exchange response:
                access_token = exchange["access_token"]
                token_type = exchange["token_type"]  # ex: Bearer
                # id_token = exchange["id_token"]
                self.logger.debug(
                    f"Received access token: {access_token}"
                )
        except Exception as err:
            raise web.HTTPForbidden(
                reason=f"Invalid Response from Token Server {err}."
            )
        try:
            # decipher the Access Token:
            # getting user information:
            options = {
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require_exp": False,
                "require_iat": False,
                "require_nbf": False,
            }
            public_key = get_public_key(
                access_token, self.tenant_id, self.discovery_oid_uri
            )
            # Validate token and extract claims
            data = jwt.decode(
                access_token,
                key=public_key,
                algorithms=["RS256", "RS384", "RS512"],
                verify=True,
                # audience=ADFS_AUDIENCE,
                audience=ADFS_DEFAULT_RESOURCE,
                issuer=ADFS_ISSUER,
                options=options,
            )
        except Exception as e:
            print('TOKEN ERROR > ', e)
            raise web.HTTPForbidden(
                reason=f"Unable to decode JWT token {e}."
            )
        try:
            # build user information:
            try:
                data = await self.get(
                    url=self.userinfo_uri,
                    token=access_token,
                    token_type=token_type,
                )
            except Exception as err:
                self.logger.error(err)
            userdata, uid = self.build_user_info(
                data, access_token
            )
            # userdata["id_token"] = id_token
            data = await self.validate_user_info(
                request, uid, userdata, access_token
            )
        except Exception as err:
            self.logger.exception(f"ADFS: Error getting User information: {err}")
            raise web.HTTPForbidden(
                reason=f"ADFS: Error with User Information: {err}"
            )
        # Redirect User to HOME
        try:
            token = data["token"]
        except (KeyError, TypeError):
            token = None
        return self.home_redirect(
            request, token=token, token_type="Bearer"
        )

    async def logout(self, request):
        # first: removing the existing session
        # second: redirect to SSO logout
        self.logger.debug(
            f"ADFS LOGOUT URI: {self.end_session_endpoint}"
        )
        return web.HTTPFound(self.end_session_endpoint)

    async def finish_logout(self, request):
        pass

    async def check_credentials(self, request):
        """Authentication and create a session."""
        return True
