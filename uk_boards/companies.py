#!/usr/bin/env python
# -*- coding: utf-8 -*-

from datetime import datetime
import logging
import os
import time

from typing import Any, Dict, Generator, Optional, Tuple, Union

from dotenv import load_dotenv

from networkx import Graph, compose, is_bipartite, is_connected

import requests
from requests.exceptions import ConnectionError

from .utils import (InternetConnectionError, get_external_ip_address,
                    DEFAULT_API_KEY_PATH)

logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=DEFAULT_API_KEY_PATH)


COMPANIES_HOUSE_URL = 'https://api.companieshouse.gov.uk'
COMPANIES_HOUSE_API_KEY_NAME = "COMPANIES_HOUSE_KEY"
COMPANIES_HOUSE_API_KEY = os.getenv(COMPANIES_HOUSE_API_KEY_NAME)

COMPANIES_HOUSE_DATE_FORMAT = '%Y-%m-%d'

COMPANIES_HOUSE_RESIGNATION_KEYWORD = 'resigned_on'

JSONDict = Dict[str, Any]

CompanyIDType = Union[str, int]

JSONItemsGenerator = Generator[Tuple[str, JSONDict], None, None]


def companies_house_query(query: str,
                          auth_key: str = COMPANIES_HOUSE_API_KEY,
                          sleep_time: int = 60,
                          url_prefix: str = COMPANIES_HOUSE_URL,
                          max_trials: int = 6,
                          params: Dict[str, Union[str, bool, int]] = None,
                          ) -> Optional[JSONDict]:
    """
    Companies House API quiery repeated when necessary, returns json if valid.

    The `auth_tuple` reflects the `(username, password)` auth
    parameters customised for Companies House api which expects the `api_key`
    in the username spot and no separate component (hence blank password).

    Args:
        query (str): a query string such as '/company/04547069'
        auth_key (str): API key which is by default loaded from a .env file
        sleep_time (int): Number of seconds to pause after query error
        url_prefix (str): Prefix of url that defaults to COMPANIES HOUSE API
        max_trials (int): Number of attempts to query, default with sleep_time
                          exceeds the 5 min max query time
        params (dict): Dictionary of usually optional parameters for search

    Returns:
        dict: A Dict of a valid JSON response (`200` response code)
        None: None returned for response codes `404`, `500` and `502`

    Raises:
        Exception: If number of `trials` is exceeded.

    Todo:
        * Cover all exceptions in tests
        * Consider better means of managing 429 Too Many Requests error
          by checking header
    """
    auth_tuple = (auth_key, "")
    trials = max_trials
    while trials:
        try:
            response = requests.get(url_prefix + query, auth=auth_tuple,
                                    params=params)
        except ConnectionError:
            raise InternetConnectionError
        if response.status_code == 200:
            return response.json()
        logger.warning(f'Status code {response.status_code} from {query}')
        if response.status_code in (403, 401):
            raise CompaniesHousePermissionError(query)
        if response.status_code == 404:
            logger.error(f'Skipping {query}')
            return None
        if response.status_code == 500:
            logger.warning(f'Will skip after {max_trials - trials} repeat')
            if trials < max_trials - 1:
                return None
        if response.status_code == 502:
            # Server error, likely an overload issue (hence adding to wait)
            logger.warning(f'Adding a {sleep_time} sec wait')
            time.sleep(sleep_time)
        logger.warning(f'Trying again in {sleep_time} seconds...')
        time.sleep(sleep_time)
        trials -= 1
    raise Exception(
            f"Failed {max_trials} attempt(s) querying "
            f"{url_prefix + query}")


class CompaniesHousePermissionError(Exception):

    """An exception for handling 403 forbidden errors."""

    def __init__(self, query: str = None, message: str = None, ) -> None:
        """Either set passed message or set to _default_error_message()."""
        self.query = query
        self.message = message or self._default_error_message()

    def __str__(self) -> str:
        return self.message

    def _default_error_message(self) -> str:
        """Try to check current IP address to raise clear permission error."""
        ip_address = get_external_ip_address()
        return (f'Query: {self.query}\nreturned a 403 (forbidden) error. '
                'If that query seems correct, check the '
                f'{COMPANIES_HOUSE_API_KEY_NAME} is set in your local .env '
                'file.\n'
                'If both are correct, check the external IP address of '
                f'this computer ({ip_address}) is included in the list of '
                'Restricted IPs on your registered Companies House API Key.')


def stringify_company_number(company_number: Union[int, str]) -> str:
    """Enforce correct company number as string of length >= 8."""
    company_number = str(company_number)
    if len(company_number) < 8:
        return company_number.rjust(8, '0')  # Shorter need preceeding '0's
    return company_number


def get_company_network(company_number: CompanyIDType = '04547069',
                        branches: int = 0,
                        include_significant_controllers: bool = False,
                        include_officers: bool = True,
                        include_edge_data: bool = False,
                        **kwargs) -> Optional[Graph]:
    """
    Recursively query a bipartite network of companies and boards.

    This function returns a bipartite NetworkX `Graph` where bipartite is 0 for
    companies and 1 for board members.

    Args:
        company_number (CompanyIDType): An int or str of a company_id
        branches (int): A positive number of hops to follow in snowball
            sampling from board members to other companies they set on
        include_significant_controllers (bool): Whether to also follow
            significant controllers as another type of board member
        include_officers (bool): Include officers in network queries.
        include_edge_data (bool): Include JSON data from
            `get_officer_appointments` as a `data` attribute for edges.
        params (dict): Dictionary of usually optional parameters for search
        **kwargs: Additional parameters to send to api calls.

            exclude_non_active_companies (bool) is for `get_company`.

            exclude_resigned_board_members (bool) is for
            `get_company_officers`.

            enforce_missing_ties (bool) is for `_get_network_branches`.

    Returns:
        Graph: A bipartite NetworkX `Graph` where bipartite is 0 for
               companies and 1 for board members.
        None: If no company is found.

    Todo:
        * 429 Too Many Requests error raised if > 600/min
        * Test officers error on company '01086582'
        * Consider removing print statement within the related loop
        * Refactor todo info into documentation
        * Add option of including network collected prior to error
        * Add options for include_significant_controllers
        * Consider enforcing inclusion of data in edges
        * Consider means of caching queries to avoid duplicates
    """
    g = Graph()
    logger.debug(f'Querying board network from {company_number}')
    company = get_company(company_number, **kwargs)
    if not company:
        return None
    logger.debug(company['company_name'])
    g.add_node(company_number, name=company['company_name'],
               bipartite=0, data=company)
    if include_officers:
        officers = get_company_officers(company_number, **kwargs)
        for officer_id, officer_data in officers:
            g.add_node(officer_id, name=officer_data['name'], bipartite=1,
                       data=officer_data)
            # g.add_edge(company_number, officer_id)
            if branches or include_edge_data:
                appointments = {related_company_id: appointment_data
                                for related_company_id, appointment_data in
                                get_officer_appointments(
                                    officer_id,
                                    company=company,
                                    company_number=company_number,
                                    officer_data=officer_data)}
            if include_edge_data:
                # consider poping key from appointments to avoid excess loop
                try:
                    appointment_data = appointments[company_number]
                except KeyError:
                    logger.warning('No appointment_data available for '
                                   f'officer {officer_data["name"]} '
                                   f'({officer_id})')
                    appointment_data = None
                officer_edge_data = {
                    'appointment_data': appointment_data,
                    'officer_data': officer_data}   # Currently a duplicate of
                                                    # officer node data
                g.add_edge(company_number, officer_id,
                           data=officer_edge_data)
            else:
                g.add_edge(company_number, officer_id)
            if branches:
                g = _get_network_branches(g, officer_id,
                                          appointments,
                                          branches=branches,
                                          root_company_id=company_number,
                                          root_company_data=company,
                                          **kwargs)
    return g


def _get_network_branches(g: Graph,
                          officer_id: str,
                          appointments: Dict[str, JSONDict],
                          root_company_id: CompanyIDType,
                          root_company_data: JSONDict,
                          # Argument ordering hopefully refactorable
                          branches: int = 0,
                          enforce_missing_ties: bool = False,
                          include_edge_data: bool = True,
                          **kwargs) -> Optional[Graph]:
    """
    Recursively expand network through individuals on multiple boards.

    Args:
        g (Graph): A graph meant to have at least one company.
        appointments (dict): Dictionary of company_id keys to appointment data.
        branches (int): Number of branches to follow.
        enforce_missing_ties (bool): Whether to add ties in cases where there
            is a record of a board membership in `get_officer_appointments`
            that doesn't appear in that company's `get_company_officers` query.
        include_edge_data (bool): Include JSON data from
            `get_officer_appointments` as a `data` attribute for edges.
        root_company_id (CompanyIDType): int or str of root `company_id` branch
            may connect to
        root_company_data (JSONDict): A dict of data on company branch may
            connect to
        **kwargs: Parameters to send to further calls of `get_company_network`.

    Returns:
        Graph: A bipartite NetworkX `Graph` where bipartite is 0 for
               companies and 1 for board members.

    Todo:
        * Expect abstract refactoring to also work with
          `significant_controllers`
        * Consider refactor to always include edge data
        * Consider means of caching queries to avoid duplicates
    """
    for related_company_id in appointments:
        if related_company_id not in g.nodes:
            related_network = get_company_network(
                related_company_id,
                branches=branches - 1,
                enforce_missing_ties=enforce_missing_ties,
                include_edge_data=include_edge_data,
                **kwargs)
            if related_network:
                g = compose(g, related_network)
                assert is_bipartite(g)
                if not is_connected(g) and enforce_missing_ties:
                    if include_edge_data:
                        g.add_edge(related_company_id, officer_id,
                                   data=appointments[related_company_id])
                    else:
                        g.add_edge(related_company_id, officer_id)
            else:
                logger.warning("Skipping company "
                               f"{related_company_id} "
                               "from board member "
                               f"{g.nodes['officer_id']['name']} "
                               f"({officer_id}) of company "
                               f"{root_company_data['company_name']} "
                               f"({root_company_id})")
    return g
#     for related_company_id, appointment_data in get_officer_appointments(
#             officer_id,
#             officer_data=officer_data,
#             company_number=company_number
#             company=company):
#         if (include_edge_data and related_company_id == company_number):
#             g[company_number][officer_id]['data'] = appointment_data
#             if not branches:
#                 break
#         elif related_company_id not in g.nodes:
#             related_network = get_company_network(
#                 related_company_id, branches=branches - 1)
#             if related_network:
#                 g = compose(g, related_network)
#                 assert is_bipartite(g)
#                 if not is_connected(g) and enforce_missing_ties:
#                     if include_edge_data:
#                         g.add_edge(related_company_id, officer_id,
#                                    data=appointment_data)
#                     else:
#                         g.add_edge(related_company_id, officer_id)
#             else:
#                 logger.warning("Skipping company "
#                                f"{related_company_id} "
#                                "from board member "
#                                f"{officer_data['name']} "
#                                f"({officer_id}) of company "
#                                f"{company['company_name']} "
#                                f"({company_number})")


def get_company(company_number: CompanyIDType = '04547069',
                exclude_non_active_companies: bool = False,
                **kwargs) -> Optional[JSONDict]:
    company_number = stringify_company_number(company_number)
    company = companies_house_query('/company/' + company_number)
    if not company:
        logger.error(f'Querying data on company {company_number} failed')
        return None
    if exclude_non_active_companies:
        if company['company_status'] != 'active':
            logger.warning(f'Excluding company {company_number} because '
                           f'status is {company["company_status"]}. '
                           f'Company name: {company["company_name"]}')
            return None
    return company


def get_company_officers(company_number: CompanyIDType = '04547069',
                         exclude_resigned_board_members: bool = False,
                         **kwargs) -> JSONItemsGenerator:
    """Yield officer_id and officer data for from company_number's board."""
    officers_query = companies_house_query(
            f'/company/{company_number}/officers')
    if not officers_query:
        logger.error(f"Error requesting officers of company {company_number}")
        # Worth considering saving error here
        return None
    for officer in officers_query['items']:
        if exclude_resigned_board_members:
            if is_inactive_board_member(officer):
                logger.debug(f"Skipping officer {officer['name']} because of "
                             f"resignation on {officer['resigned_on']}")
                continue
        officer_id = officer['links']['officer']['appointments'].split('/')[2]
        logger.debug(f'{company_number} {officer["name"]} {officer_id}')
        yield officer_id, officer


def get_officer_appointments(officer_id: str, **kwargs) -> JSONItemsGenerator:
    """Query officer appointments and yield company_id, appointment_data."""
    appointments = companies_house_query(
            f'/officers/{officer_id}/appointments')
    if not appointments:
        if {'company', 'company_number', 'officer_data'} <= kwargs:
            company, company_number, officer_data = (kwargs['company'],
                                                     kwargs['company_number'],
                                                     kwargs['officer_data'])
            logger.error("Error requesting appointments of board "
                         f"member {officer_data['name']} ({officer_id}) of "
                         f"company {company['company_name']} "
                         f"({company_number})")
        else:
            logger.error("Error requesting appointments of board "
                         f"member {officer_id}")
        # Worth considering saving error here
        return None
    for appointment in appointments['items']:
        yield appointment['appointed_to']['company_number'], appointment


def is_inactive_board_member(officer: dict) -> bool:
    """Return boolean of whether officer is no longer a board member."""
    return (COMPANIES_HOUSE_RESIGNATION_KEYWORD in officer and
            datetime.strptime(officer[COMPANIES_HOUSE_RESIGNATION_KEYWORD],
                              COMPANIES_HOUSE_DATE_FORMAT) < datetime.today())


def filter_active_board_members(g: Graph) -> Graph:
    """Return a graph with only active board members."""
    subgraph = g.copy()
    inactive_board_members = [id for id, data in subgraph.nodes(data=True) if
                              data['bipartite'] == 1 and
                              is_inactive_board_member(data['data'])]
    subgraph.remove_nodes_from(inactive_board_members)
    return subgraph
