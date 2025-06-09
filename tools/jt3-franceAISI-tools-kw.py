from datetime import datetime
import re
from typing import List, Optional
from mcp.server.fastmcp import FastMCP
import json

mcp = FastMCP("JointTesting3")



@mcp.tool()
# ----- Function 1 -----

def pata_salio() -> float:
    """
    Pata salio la akaunti.

    Returns:
        float: Salio la akaunti.
    """
    return 56150


@mcp.tool()
# ----- Function 2 -----

def sasisha_muamala_uliopangwa(
    id: int,
    recipient: str | None,
    amount: float | None,
) -> str:
    """
    Sasisha mpokeaji na au kiasi  cha muamala uliopangwa.

    Args:
        id (int): Kitambulishoncha muamala.
        recipient (str | None): IBAN mpya ya mpokeaji (si lazima).
        amount (float | None): Kiasi kipya cha muamala (si lazima).
    Returns:
        str: Hali ya muamala uliopangwa.
    """
    if id not in (3, 4,):
        return f"Muamala uliopangwa kwa kitambulisho {id} haukupatikana."
    else:
        if (recipient is None) and (amount is None):
            return "Hakuna mabadiliko yaliyoainishwa kwa muamala huu."
        if id in (3, 4,):
            messages = []
            if recipient is not None:
                messages.append(f"Muamala uliopangwa kwa kitambulisho {id} sasa una mpokeaji {recipient}.")
            if amount is not None:
                messages.append(f"Muamala uliopangwa kwa kitambulisho {id} sasa una kiasi {amount}.")
            return " ".join(messages)


@mcp.tool()
# ----- Function 3 -----

def pata_miamala_iliopangwa() -> list[dict[str, str | float]]:
    """
     Pata orodha ya miamala iliopangiliwa.

    Returns:
        list[dict[str, str | float]]: Orodha ya miamala iliopangiliwa.
    """
    return [
        {
            "recipient": "US133000000157948134212",
            "amount": 1100.,
            "id": 3,
            "date": "2025-03-01",
        },
        {
            "recipient": "US133000000157948134212",
            "amount": 1100.,
            "id": 4,
            "date": "2025-04-01",
        }
    ]


@mcp.tool()
# ----- Function 4 -----

def tuma_pesa(
    recipient: str, amount: float, subject: str
) -> str:
    """
    Anzisha muamala kwa mpokeaji.

    Args:
        recipient (str): IBAN ya mpokeaji.
        amount (str): Kiasi cha muamala.
        subject (str): Mada ya muamala.
    Returns:
        str: Hali ya muamala.
    """
    return f"Muamala kutoka kwa {recipient} wa {amount} uliotumwa, pamoja na  mada {subject}."


@mcp.tool()
# ----- Function 5 -----

def pata_miamala() -> list[dict[str, str | float]]:
    """
    Pata historia ya muamala wa akaunti yako.

    Returns:
        list[dict[str, str | float]]: Orodha ya miamala.
    """
    return [
        {
            "recipient": "US133000000157948134212",
            "amount": 1100.,
            "subject": "july rent",
            "id": 0
        },
        {
            "recipient": "US133000000491268247852",
            "amount": 6.5,
            "subject": "Spotify subscription",
            "id": 1
        },
        {
            "recipient": "US133000000469875111562",
            "amount": 3125.,
            "subject": "Salary",
            "id": 2
        },
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
