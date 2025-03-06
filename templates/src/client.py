from typing import Any, Dict, Mapping, Optional, Tuple

import backoff
import requests
from requests import session
from requests.exceptions import Timeout, ConnectionError, ChunkedEncodingError
from singer import get_logger, metrics

from {{tap_name}}.exceptions import ERROR_CODE_EXCEPTION_MAPPING, {{config.tap_name}}Error, {{config.tap_name}}BackoffError

LOGGER = get_logger()
REQUEST_TIMEOUT = 300

def raise_for_error(response: requests.Response) -> None:
    """Raises the associated response exception. Takes in a response object,
    checks the status code, and throws the associated exception based on the
    status code.

    :param resp: requests.Response object
    """
    try:
        response_json = response.json()
    except Exception: # pylint: disable=broad-except
        response_json = {}
    if response.status_code != [200, 201, 204]:
        if response_json.get("error"):
            message = "HTTP-error-code: {}, Error: {}".format(response.status_code, response_json.get("error"))
        else:
            message = "HTTP-error-code: {}, Error: {}".format(
                response.status_code,
                response_json.get("message", ERROR_CODE_EXCEPTION_MAPPING.get(
                    response.status_code, {}).get("message", "Unknown Error")))
        exc = ERROR_CODE_EXCEPTION_MAPPING.get(
            response.status_code, {}).get("raise_exception", {{config.tap_name}}Error)
        raise exc(message, response) from None

class Client:
    """
    A Wrapper class.
    ~~~
    Performs:
     - Authentication
     - Response parsing
     - HTTP Error handling and retry
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = config
        self._session = session()
        self.base_url = "{{ config.base_url if config.base_url else "" }}"

        # Set and pass request timeout to config param `request_timeout` value.
        config_request_timeout = config.get("request_timeout")
        if config_request_timeout and float(config_request_timeout):
            self.request_timeout = float(config_request_timeout)
        else:
            self.request_timeout = REQUEST_TIMEOUT # If value is 0,"0","" or not passed then it set default to 300 seconds.

    def __enter__(self):
        self.check_api_credentials()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self._session.close()

    def check_api_credentials(self) -> None:
        pass

    def authenticate(self, headers: Dict, params: Dict) -> Tuple[Dict, Dict]:
        """Authenticates the request with the token"""
        headers["{{ config.auth_header_key if config.auth_header_key else auth_header_key }}"] = self.config["{{ config.auth_config_key if config.auth_config_key else auth_config_key }}"]
        return headers, params

    def get(self, endpoint: str, params: Dict, headers: Dict, path: str = None) -> Any:
        """Calls the make_request method with a prefixed method type `GET`"""
        endpoint = endpoint or f"{self.base_url}/{path}"
        headers, params = self.authenticate(headers, params)
        return self.__make_request("GET", endpoint, headers=headers, params=params, timeout=self.request_timeout)

    def post(self, endpoint: str, params: Dict, headers: Dict, body: Dict, path: str = None) -> Any:
        """Calls the make_request method with a prefixed method type `POST`"""
        # pylint: disable=R0913
        headers, params = self.authenticate(headers, params)
        self.__make_request("POST", endpoint, headers=headers, params=params, data=body, timeout=self.request_timeout)


    @backoff.on_exception(
        wait_gen=backoff.expo,
        exception=(
            ConnectionResetError,
            ConnectionError,
            ChunkedEncodingError,
            Timeout,
            {{config.tap_name}}BackoffError
        ),
        max_tries=5,
        factor=2,
    )
    def __make_request(self, method: str, endpoint: str, **kwargs) -> Optional[Mapping[Any, Any]]:
        """
        Performs HTTP Operations
        Args:
            method (str): represents the state file for the tap.
            endpoint (str): url of the resource that needs to be fetched
            params (dict): A mapping for url params eg: ?name=Avery&age=3
            headers (dict): A mapping for the headers that need to be sent
            body (dict): only applicable to post request, body of the request

        Returns:
            Dict,List,None: Returns a `Json Parsed` HTTP Response or None if exception
        """
        with metrics.http_request_timer(endpoint) as timer:
            response = self._session.request(method, endpoint, **kwargs)
            raise_for_error(response)

        return response.json()
