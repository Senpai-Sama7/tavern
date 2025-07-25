"""
Tavern REST Request Plugin

This module provides the REST request functionality for the Tavern testing framework.
It handles HTTP request creation, formatting, and execution for REST API testing.

The module contains classes and functions for building and sending HTTP requests
with various authentication methods, headers, and payload formats for REST APIs.
"""

import contextlib
import json
import logging
import warnings
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from itertools import filterfalse, tee
from typing import ClassVar, Optional
from urllib.parse import quote_plus

import requests
from box.box import Box
from requests.cookies import cookiejar_from_dict
from requests.utils import dict_from_cookiejar

from tavern._core import exceptions
from tavern._core.dict_util import check_expected_keys, deep_dict_merge, format_keys
from tavern._core.extfunctions import update_from_ext
from tavern._core.general import valid_http_methods
from tavern._core.pytest.config import TestConfig
from tavern._core.report import attach_yaml
from tavern._plugins.rest.files import get_file_arguments, guess_filespec
from tavern.request import BaseRequest

logger: logging.Logger = logging.getLogger(__name__)


def get_request_args(rspec: dict, test_block_config: TestConfig) -> dict:
    """Format the test spec given values inthe global config

    Todo:
        Add similar functionality to validate/save $ext functions so input
        can be generated from a function

    Args:
        rspec: Test spec
        test_block_config: Test block config

    Returns:
        Formatted test spec

    Raises:
        BadSchemaError: Tried to pass a body in a GET request
    """

    if "method" not in rspec:
        logger.debug("Using default GET method")
        rspec["method"] = "GET"

    if "headers" not in rspec:
        rspec["headers"] = {}

    content_keys = ["data", "json", "files", "file_body"]

    in_request = [c for c in content_keys if c in rspec]
    if len(in_request) > 1:
        # Explicitly raise an error here
        # From requests docs:
        # Note, the json parameter is ignored if either data or files is passed.
        # However, we allow the data + files case, as requests handles it correctly
        if set(in_request) != {"data", "files"}:
            raise exceptions.BadSchemaError(
                "Can only specify one type of request data in HTTP request (tried to "
                "send {})".format(" and ".join(in_request))
            )

    normalised_headers = {k.lower(): v for k, v in rspec["headers"].items()}

    def get_header(name):
        return normalised_headers.get(name, None)

    content_header = get_header("content-type")
    encoding_header = get_header("content-encoding")

    if "files" in rspec:
        if content_header:
            logger.warning(
                "Tried to specify a content-type header while sending multipart "
                "files - this will be ignored"
            )
            rspec["headers"] = {
                i: j
                for i, j in normalised_headers.items()
                if i.lower() != "content-type"
            }

    fspec = format_keys(rspec, test_block_config.variables)

    if fspec["method"] not in valid_http_methods:
        raise exceptions.BadSchemaError(
            "Unknown HTTP method {}".format(
                fspec["method"]
            )
        )

    # If the user is using the file_body key, try to guess what type of file/encoding it is.
    filename = fspec.get("file_body")
    if filename:
        with ExitStack() as stack:
            file_spec, group_name = guess_filespec(filename, stack, test_block_config)

            # Group name doesn't matter here as it's a single file
            if group_name:
                logger.warning(
                    f"'group_name' for the 'file_body' key was specified as "
                    f"'{group_name}' but this will be ignored "
                )

            fspec["file_body"] = filename
            if len(file_spec) == 2:
                logger.debug(
                    "No content type or encoding inferred from file_body for %s",
                    filename,
                )

            if len(file_spec) >= 3:
                inferred_content_type = file_spec[2]
                if content_header:
                    logger.info(
                        "inferred content type '%s' from %s, but using user "
                        "specified content type '%s'",
                        inferred_content_type,
                        filename,
                        content_header,
                    )
                else:
                    fspec["headers"]["content-type"] = inferred_content_type

            if len(file_spec) == 4:
                inferred_content_encoding = file_spec[3]
                if encoding_header:
                    logger.info(
                        "inferred content encoding '%s' from %s, but using user specified encoding '%s",
                        inferred_content_encoding,
                        filename,
                        encoding_header,
                    )
                else:
                    fspec["headers"].update(**inferred_content_encoding)

    #########################################

    request_args = {}

    def add_request_args(keys, optional):
        for key in keys:
            try:
                request_args[key] = fspec[key]
            except KeyError:
                if optional or (key in request_args):
                    continue

                # This should never happen
                raise

    # Ones that are required and are enforced to be present by the schema
    required_in_file = ["method", "url"]

    add_request_args(["file_body"], True)
    add_request_args(required_in_file, False)
    add_request_args(RestRequest.optional_in_file, True)

    if "auth" in fspec:
        request_args["auth"] = tuple(fspec["auth"])

    if "cert" in fspec:
        if isinstance(fspec["cert"], list):
            request_args["cert"] = tuple(fspec["cert"])

    if "timeout" in fspec:
        # Needs to be a tuple, it being a list doesn't work
        if isinstance(fspec["timeout"], list):
            request_args["timeout"] = tuple(fspec["timeout"])

    # If there's any nested json in parameters, urlencode it
    # if you pass nested json to 'params' then requests silently fails and just
    # passes the 'top level' key, ignoring all the nested json. I don't think
    # there's a standard way to do this, but urlencoding it seems sensible
    # eg https://openid.net/specs/openid-connect-core-1_0.html#ClaimsParameter
    # > ...represented in an OAuth 2.0 request as UTF-8 encoded JSON (which ends
    # > up being form-urlencoded when passed as an OAuth parameter)
    for key, value in request_args.get("params", {}).items():
        if not isinstance(value, str):
            if key == "$ext":
                logger.debug("Skipping converting of ext function (%s)", value)
                continue

        if isinstance(value, dict):
            request_args["params"][key] = quote_plus(json.dumps(value))

    optional = {"verify", "stream"}

    for key in optional:
        if key in fspec:
            request_args[key] = fspec[key]

    # TODO
    # requests takes all of these - we need to parse the input to get them
    # "cookies",

    # These verbs _can_ send a body but the body _should_ be ignored according
    # to the specs - some info here:
    # https://developer.mozilla.org/en-US/docs/Web/HTTP/Methods
    if request_args["method"] in ["GET", "HEAD", "OPTIONS"]:
        if any(i in request_args for i in ["json", "data"]):
            warnings.warn(  # noqa
                "You are trying to send a body with a HTTP verb that has no semantic use for it",
                RuntimeWarning,
            )

    return request_args


@contextlib.contextmanager
def _set_cookies_for_request(session: requests.Session, request_args: Mapping):
    """
    Possibly reset session cookies for a single request then set them back.
    If no cookies were present in the request arguments, do nothing.

    This does not use try/finally because if it fails then we don't care about
    the cookies anyway

    Args:
        session: Current session
        request_args: current request arguments
    """
    if "cookies" in request_args:
        old_cookies = dict_from_cookiejar(session.cookies)
        session.cookies = cookiejar_from_dict({})
        yield
        session.cookies = cookiejar_from_dict(old_cookies)
    else:
        yield


def _check_allow_redirects(rspec: dict, test_block_config: TestConfig):
    """
    Check for allow_redirects flag in settings/stage

    Args:
        rspec: request dictionary
        test_block_config: config available for test

    Returns:
        Whether to allow redirects for this stage or not
    """
    # By default, don't follow redirects
    allow_redirects = False

    # Then check to see if we should follow redirects based on settings
    global_follow_redirects = test_block_config.follow_redirects
    if global_follow_redirects is not None:
        allow_redirects = global_follow_redirects

    # ... and test flags
    test_follow_redirects = rspec.pop("follow_redirects", None)
    if test_follow_redirects is not None:
        if global_follow_redirects is not None:
            logger.info(
                "Overriding global follow_redirects setting of %s with test-level specification of %s",
                global_follow_redirects,
                test_follow_redirects,
            )
        allow_redirects = test_follow_redirects

    logger.debug("Allow redirects in stage: %s", allow_redirects)

    return allow_redirects


def _read_expected_cookies(
    session: requests.Session, rspec: Mapping, test_block_config: TestConfig
) -> Optional[dict]:
    """
    Read cookies to inject into request, ignoring others which are present

    Args:
        session: session object
        rspec: test spec
        test_block_config: config available for test

    Returns:
        cookies to use in request, if any
    """
    # Need to do this down here - it is separate from getting request args as
    # it depends on the state of the session
    existing_cookies = session.cookies.get_dict()
    cookies_to_use = format_keys(
        rspec.get("cookies", None), test_block_config.variables
    )

    if cookies_to_use is None:
        logger.debug("No cookies specified in request, sending all")
        return None
    elif cookies_to_use in ([], {}):
        logger.debug("Not sending any cookies with request")
        return {}

    def partition(pred, iterable):
        """From itertools documentation"""
        t1, t2 = tee(iterable)
        return list(filterfalse(pred, t1)), list(filter(pred, t2))

    # Cookies are either a single list item, specitying which cookie to send, or
    # a mapping, specifying cookies to override
    expected, extra = partition(lambda x: isinstance(x, dict), cookies_to_use)

    missing = set(expected) - set(existing_cookies.keys())

    if missing:
        logger.error("Missing cookies")
        raise exceptions.MissingCookieError(
            f"Tried to use cookies '{expected}' in request but only had '{existing_cookies}' available"
        )

    # 'extra' should be a list of dictionaries - merge them into one here
    from_extra = {k: v for mapping in extra for (k, v) in mapping.items()}

    if len(extra) != len(from_extra):
        logger.error("Duplicate cookie override values specified")
        raise exceptions.DuplicateCookieError(
            "Tried to override the value of a cookie multiple times in one request"
        )

    overwritten = [i for i in expected if i in from_extra]

    if overwritten:
        logger.error("Duplicate cookies found in request")
        raise exceptions.DuplicateCookieError(
            f"Asked to use cookie {overwritten} from previous request but also redefined it as {from_extra}"
        )

    from_cookiejar = {c: existing_cookies.get(c) for c in expected}

    return deep_dict_merge(from_cookiejar, from_extra)


class RestRequest(BaseRequest):
    """REST request implementation for Tavern.

    This class handles the creation and execution of REST HTTP requests.
    It supports various HTTP methods, headers, authentication, and file uploads.
    """
    optional_in_file: ClassVar[list[str]] = [
        "json",
        "data",
        "params",
        "headers",
        "files",
        "timeout",
        "cert",
        # Ideally this would just be passed through but requests seems to error
        # if we pass a list instead of a tuple, so we have to manually convert
        # it further down
        # "auth"
    ]

    _request_args: Box

    def __init__(
        self, session: requests.Session, rspec: dict, test_block_config: TestConfig
    ) -> None:
        """Prepare request

        Args:
            session: existing session
            rspec: test spec
            test_block_config: Any configuration for this the block of
                tests

        Raises:
            UnexpectedKeysError: If some unexpected keys were used in the test
                spec. Only valid keyword args to requests can be passed
        """

        if rspec.pop("clear_session_cookies", False):
            session.cookies.clear_session_cookies()

        expected = {
            "method",
            "url",
            "headers",
            "data",
            "params",
            "auth",
            "json",
            "verify",
            "files",
            "file_body",
            "stream",
            "timeout",
            "cookies",
            "cert",
            # "hooks",
            "follow_redirects",
        }

        check_expected_keys(expected, rspec)

        request_args = get_request_args(rspec, test_block_config)
        update_from_ext(
            request_args,
            RestRequest.optional_in_file + ["url"],
        )

        # Used further down, but pop it asap to avoid unwanted side effects
        file_body: Optional[str] = request_args.pop("file_body", None)

        # If there was a 'cookies' key, set it in the request
        expected_cookies = _read_expected_cookies(session, rspec, test_block_config)
        if expected_cookies is not None:
            logger.debug("Sending cookies %s in request", expected_cookies.keys())
            request_args.update(cookies=expected_cookies)

        # Check for redirects
        request_args.update(
            allow_redirects=_check_allow_redirects(rspec, test_block_config)
        )

        logger.debug("Request args: %s", request_args)

        self._request_args = Box(request_args)

        # There is no way using requests to make a prepared request that will
        # not follow redirects, so instead we have to do this. This also means
        # that we can't have the 'pre-request' hook any more because we don't
        # create a prepared request.

        def prepared_request():
            # If there are open files, create a context manager around each so
            # they will be closed at the end of the request.
            with ExitStack() as stack:
                stack.enter_context(_set_cookies_for_request(session, request_args))

                # These are mutually exclusive
                if file_body:
                    # Any headers will have been set in the above function
                    file = stack.enter_context(open(file_body, "rb"))
                    self._request_args.update(data=file)
                else:
                    files = get_file_arguments(
                        self._request_args, stack, test_block_config
                    )
                    if files:
                        logger.debug("Sending %d files in request", len(files["files"]))
                        self._request_args.update(files)

                headers = self._request_args.get("headers", {})
                for k, v in headers.items():
                    headers[str(k)] = str(v)

                return session.request(**self._request_args)

        self._prepared: Callable[[], requests.Response] = prepared_request

    def run(self) -> requests.Response:
        """Runs the prepared request and times it

        Returns:
            response object
        """

        attach_yaml(
            self._request_args,
            name="rest_request",
        )

        try:
            return self._prepared()
        except requests.exceptions.RequestException as e:
            logger.exception("Error running prepared request")
            raise exceptions.RestRequestException from e

    @property
    def request_vars(self) -> Box:
        return self._request_args
