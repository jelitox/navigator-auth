"""Django Session Backend.

Navigator Authentication using API Token
description: Single API Token Authentication
"""
from typing import List
import jwt
from aiohttp import web
from navigator_session import get_session
from navigator_auth.exceptions import AuthException, InvalidAuth
from navigator_auth.conf import (
    AUTH_JWT_ALGORITHM,
    AUTH_TOKEN_ISSUER,
    AUTH_TOKEN_SECRET
)
# Authenticated Entity
from navigator_auth.identities import AuthUser, Program
from .abstract import BaseAuthBackend

class TokenUser(AuthUser):
    tenant: str
    programs: List[Program]

class TokenAuth(BaseAuthBackend):
    """API Token Authentication Handler."""

    _pool = None
    _ident: AuthUser = TokenUser

    def configure(self, app, router):
        super(TokenAuth, self).configure(app, router)

    async def on_startup(self, app: web.Application):
        """Used to initialize Backend requirements.
        """

    async def on_cleanup(self, app: web.Application):
        """Used to cleanup and shutdown any db connection.
        """

    async def get_payload(self, request):
        token = None
        tenant = None
        id = None
        try:
            if "Authorization" in request.headers:
                try:
                    scheme, id = (
                        request.headers.get("Authorization").strip().split(" ", 1)
                    )
                except ValueError:
                    raise AuthException(
                        "Invalid authorization Header",
                        status=400
                    )
                if scheme != self.scheme:
                    raise AuthException(
                        "Invalid Authorization Scheme",
                        status=400
                    )
                try:
                    tenant, token = id.split(":")
                except ValueError:
                    token = id
        except Exception as err:
            self.logger.exception(f"TokenAuth: Error getting payload: {err}")
            return None
        return [tenant, token]

    async def reconnect(self):
        if not self.connection or not self.connection.is_connected():
            await self.connection.connection()

    async def authenticate(self, request):
        """ Authenticate, refresh or return the user credentials."""
        try:
            tenant, token = await self.get_payload(request)
            self.logger.debug(f"Tenant ID: {tenant}")
        except Exception as err:
            raise AuthException(
                err, status=400
            ) from err
        if not token:
            raise InvalidAuth(
                "Invalid Credentials", status=401
            )
        else:
            payload = jwt.decode(
                token, AUTH_TOKEN_SECRET, algorithms=[AUTH_JWT_ALGORITHM], leeway=30
            )
            # self.logger.debug(f"Decoded Token: {payload!s}")
            data = await self.check_token_info(request, tenant, payload)
            if not data:
                raise InvalidAuth(
                    f"Invalid Session: {token!s}", status=401
                )
            # getting user information
            # making validation
            try:
                u = data["name"]
                username = data["partner"]
                grants = data["grants"]
                programs = data["programs"]
            except KeyError as err:
                print(err)
                raise InvalidAuth(
                    f"Missing attributes for Partner Token: {err!s}",
                    status=401
                ) from err
            # TODO: Validate that partner (tenants table):
            try:
                userdata = dict(data)
                id = data["name"]
                user = {
                    "name": data["name"],
                    "partner": username,
                    "issuer": AUTH_TOKEN_ISSUER,
                    "programs": programs,
                    "grants": grants,
                    "tenant": tenant,
                    "id": data["name"],
                    "user_id": id,
                }
                userdata[self.session_key_property] = id
                usr = await self.create_user(userdata)
                usr.id = id
                usr.set(self.username_attribute, id)
                usr.programs = programs
                usr.tenant = tenant
                self.logger.debug(f'User Created: {usr}')
                token = self.create_jwt(data=user)
                usr.access_token = token
                # saving user-data into request:
                await self.remember(
                    request, id, userdata, usr
                )
                return {
                    "token": f"{tenant}:{token}",
                    **user
                }
            except Exception as err:
                self.logger.exception(f'DjangoAuth: Authentication Error: {err}')
                return False

    async def check_credentials(self, request):
        pass

    async def check_token_info(self, request, tenant, payload):
        try:
            name = payload["name"]
            partner = payload["partner"]
        except KeyError as err:
            return False
        sql = """
        SELECT name, partner, grants, programs FROM auth.partner_keys
        WHERE name=$1 AND partner=$2
        AND enabled = TRUE AND revoked = FALSE AND $3= ANY(programs)
        """
        app = request.app
        pool = app['authdb']
        try:
            result = None
            async with await pool.acquire() as conn:
                result, error = await conn.queryrow(sql, name, partner, tenant)
                if error or not result:
                    return False
                else:
                    return result
        except Exception as err:
            self.logger.exception(err)
            return False

    async def auth_middleware(self, app, handler):
        async def middleware(request):
            self.logger.debug(f'MIDDLEWARE: {self.__class__.__name__}')
            request.user = None
            try:
                if request.get('authenticated', False) is True:
                    # already authenticated
                    return await handler(request)
            except KeyError:
                pass
            tenant, jwt_token = await self.get_payload(request)
            if jwt_token:
                try:
                    payload = jwt.decode(
                        jwt_token, AUTH_TOKEN_SECRET, algorithms=[AUTH_JWT_ALGORITHM], leeway=30
                    )
                    # self.logger.debug(f"Decoded Token: {payload!s}")
                    result = await self.check_token_info(request, tenant, payload)
                    if not result:
                        raise web.HTTPForbidden(
                            reason="API Key Not Authorized",
                        )
                    else:
                        request[self.session_key_property] = payload['name']
                        # TRUE because if data doesnt exists, returned
                        session = await get_session(request, payload, new = True)
                        session["grants"] = result["grants"]
                        session["partner"] = result["partner"]
                        session["tenant"] = tenant
                        try:
                            # request.user = session.decode('name')
                            request.user = session.decode('user')
                            request.user.is_authenticated = True
                        except KeyError:
                            pass
                        # print('USER> ', request.user, type(request.user))
                        request['authenticated'] = True
                except (jwt.exceptions.ExpiredSignatureError) as err:
                    self.logger.error(f"TokenAuth: token expired: {err!s}")
                    raise web.HTTPForbidden(
                        reason=f"TokenAuth: token expired: {err!s}"
                    )
                except (jwt.exceptions.InvalidSignatureError) as err:
                    self.logger.error(f"Invalid Credentials: {err!r}")
                    raise web.HTTPForbidden(
                        reason=f"TokenAuth: Invalid or missing Credentials: {err!r}"
                    )
                except (jwt.exceptions.DecodeError, jwt.exceptions.InvalidTokenError) as err:
                    self.logger.error(f"Invalid authorization token: {err!r}")
                    raise web.HTTPForbidden(
                        reason=f"TokenAuth: Invalid authorization token: {err!r}"
                    )
                except Exception as err:
                    self.logger.exception(f"Error on Token Middleware: {err}")
                    raise web.HTTPClientError(
                        reason=f"Error on TokenAuth Middleware: {err}"
                    )
            return await handler(request)

        return middleware
