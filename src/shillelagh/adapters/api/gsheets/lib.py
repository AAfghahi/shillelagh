"""Helper functions for the GSheets adapter."""
import datetime
import itertools
import string
import urllib.parse
from functools import partial
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple
from typing import Type

import google.oauth2.credentials
import google.oauth2.service_account
from google.auth.credentials import Credentials

from shillelagh.adapters.api.gsheets.fields import GSheetsBoolean
from shillelagh.adapters.api.gsheets.fields import GSheetsDate
from shillelagh.adapters.api.gsheets.fields import GSheetsDateTime
from shillelagh.adapters.api.gsheets.fields import GSheetsFloat
from shillelagh.adapters.api.gsheets.fields import GSheetsString
from shillelagh.adapters.api.gsheets.fields import GSheetsTime
from shillelagh.adapters.api.gsheets.types import SyncMode
from shillelagh.adapters.api.gsheets.typing import QueryResultsColumn
from shillelagh.adapters.api.gsheets.typing import QueryResultsError
from shillelagh.adapters.api.gsheets.typing import UrlArgs
from shillelagh.exceptions import ProgrammingError
from shillelagh.fields import Field
from shillelagh.fields import Order
from shillelagh.filters import Equal
from shillelagh.filters import Filter
from shillelagh.filters import Range
from shillelagh.typing import Row


# Google API scopes for authentication
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://spreadsheets.google.com/feeds",
]


def get_field(
    col: QueryResultsColumn,
    timezone: Optional[datetime.tzinfo],
) -> Field:
    """
    Return a Shillelagh `Field` from a Google Chart API results column.
    """
    type_map: Dict[str, Tuple[Type[Field], List[Type[Filter]]]] = {
        "string": (GSheetsString, [Equal]),
        "number": (GSheetsFloat, [Range]),
        "boolean": (GSheetsBoolean, [Equal]),
        "date": (GSheetsDate, [Range]),
        "datetime": (partial(GSheetsDateTime, timezone=timezone), [Range]),  # type: ignore
        "timeofday": (GSheetsTime, [Range]),
    }
    class_, filters = type_map.get(col["type"], (GSheetsString, [Equal]))
    return class_(
        filters=filters,
        order=Order.ANY,
        exact=True,
    )


def format_error_message(errors: List[QueryResultsError]) -> str:
    """
    Return an error message from a Google Chart API error response.
    """
    return "\n\n".join(error["detailed_message"] for error in errors)


def get_url(
    uri: str,
    headers: int = 0,
    gid: int = 0,
    sheet: Optional[str] = None,
) -> str:
    """
    Return the Google Chart API URL given the spreadsheet URL.
    """
    parts = urllib.parse.urlparse(uri)

    # strip /edit
    path = parts.path[: -len("/edit")] if parts.path.endswith("/edit") else parts.path

    # add the gviz endpoint
    path = "/".join((path.rstrip("/"), "gviz/tq"))

    query_string = urllib.parse.parse_qs(parts.query)
    if "headers" in query_string:
        headers = int(query_string["headers"][-1])
    if "gid" in query_string:
        gid = int(query_string["gid"][-1])
    if "sheet" in query_string:
        sheet = query_string["sheet"][-1]

    if parts.fragment.startswith("gid="):
        gid = int(parts.fragment[len("gid=") :])

    args: UrlArgs = {}
    if headers > 0:
        args["headers"] = headers
    if sheet is not None:
        args["sheet"] = sheet
    else:
        args["gid"] = gid
    params = urllib.parse.urlencode(args)

    return urllib.parse.urlunparse(
        (parts.scheme, parts.netloc, path, None, params, None),
    )


def get_sync_mode(uri: str) -> Optional[SyncMode]:
    """
    Extract the synchronization mode from the sheet URI.

    User can specify a custom synchronization mode by manually adding the
    `sync_mode` query argument. The mode can be specified using the
    enum names (lower or uppercase) or their corresponding numbers. These
    are all equivalent:

        ?sync_mode=BATCH
        ?sync_mode=batch
        ?sync_mode=3

    """
    parts = urllib.parse.urlparse(uri)
    query_string = urllib.parse.parse_qs(parts.query)
    if "sync_mode" not in query_string:
        return None

    parameter = query_string["sync_mode"][-1].upper()
    try:
        sync_mode = SyncMode[parameter]
    except KeyError:
        try:
            sync_mode = SyncMode(int(parameter))
        except ValueError as ex:
            raise ProgrammingError(f"Invalid sync mode: {parameter}") from ex

    return sync_mode


def gen_letters() -> Iterator[str]:
    """
    Generate column labels.

    This generator produces column labels for sheets: "A", "B", ..., "Z", "AA",
    "AB", etc.
    """
    letters = ["A"]
    index = 0
    while True:
        yield "".join(letters)

        index += 1
        if index == len(string.ascii_uppercase):
            letters[-1] = "A"
            letters.append("A")
            index = 0
        else:
            letters[-1] = string.ascii_uppercase[index]


def get_index_from_letters(letters: str) -> int:
    """
    Return the index of a given column label.

        >>> get_index_from_letters("A")
        0
        >>> get_index_from_letters("AA")
        26

    """
    base26 = reversed([string.ascii_uppercase.index(letter) + 1 for letter in letters])
    return (
        sum(
            value * (len(string.ascii_uppercase) ** i) for i, value in enumerate(base26)
        )
        - 1
    )


def get_values_from_row(
    row: Row,
    columns: Dict[str, Field],
    column_map: Dict[str, str],
) -> List[Any]:
    """
    Convert a `Row` into a list of values.

    This takes into consideration empty columns. For example:

        >>> column_map = {"country": "A", "cnt": "C"}  # empty column B
        >>> row = {"country": "BR", "cnt": 10}
        >>> get_values_from_row(row, column_map)
        ['BR', None, 10]
    """
    # convert row to unformatteed
    row = {k: columns[k].to_unformatted(v) for k, v in row.items() if k in columns}

    # convert column names to column labels
    row = {column_map[k]: v for k, v in row.items() if k in column_map}

    # complete row with missing columns
    n_cols = get_index_from_letters(max(column_map.values())) + 1
    return [row.get(column) for column in itertools.islice(gen_letters(), n_cols)]


def get_credentials(
    access_token: Optional[str],
    service_account_file: Optional[str],
    service_account_info: Optional[Dict[str, Any]],
    subject: Optional[str],
) -> Optional[Credentials]:
    """
    Return a set of credentials.

    The user can provide either an OAuth token directly, the location of a service
    account file, or the contents of the service account directly. When passing
    credentials from a service account the user can also specify a "subject", used
    to impersonate a given user.
    """
    if access_token:
        return google.oauth2.credentials.Credentials(access_token)

    if service_account_file:
        return google.oauth2.service_account.Credentials.from_service_account_file(
            service_account_file,
            scopes=SCOPES,
            subject=subject,
        )

    if service_account_info:
        return google.oauth2.service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=SCOPES,
            subject=subject,
        )

    return None
