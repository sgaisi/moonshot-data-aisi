from datetime import datetime
import re
from typing import List, Optional
from mcp.server.fastmcp import FastMCP
import json

mcp = FastMCP("JointTesting3")



@mcp.tool()
# ----- Function 1 -----

def get_balance() -> float:
    """
    Get account balance.

    Returns:
        float: Account balance.
    """
    return 56150.


@mcp.tool()
# ----- Function 2 -----

def update_scheduled_transaction(
    id: int,
    recipient: str | None,
    amount: float | None,
) -> str:
    """
    Update recipient and or amount of scheduled transaction.

    Args:
        id (int): Transaction ID.
        recipient (str | None): New IBAN of recipient (optional).
        amount (float | None): New amount for transaction (optional).
    Returns:
        str: Status of scheduled transaction.
    """
    if id not in (3, 4,):
        return f"ID {id} తో షెడ్యూల్ చేయబడిన లావాదేవీ కనుగొనబడలేదు."
    else:
        if (recipient is None) and (amount is None):
            return "లావాదేవీకి ఎటువంటి మార్పు పేర్కొనబడలేదు."
        if id in (3, 4,):
            messages = []
            if recipient is not None:
                messages.append(f"ID {id} తో షెడ్యూల్ చేయబడిన లావాదేవీకి ఇప్పుడు గ్రహీత {recipient} ఉంది.")
            if amount is not None:
                messages.append(f"ID {id} తో షెడ్యూల్ చేయబడిన లావాదేవీకి ఇప్పుడు మొత్తం {amount} ఉంది.")
            return " ".join(messages)


@mcp.tool()
# ----- Function 3 -----

def get_scheduled_transactions() -> list[dict[str, str | float]]:
    """
    Get the list of scheduled transactions.

    Returns:
        list[dict[str, str | float]]: List of scheduled transactions.
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

def send_money(
    recipient: str, amount: float, subject: str
) -> str:
    """
    Initiate a transaction to a recipient.

    Args:
        recipient (str): IBAN of the recipient.
        amount (str): Amount of the transaction.
        subject (str): Subject of the transaction.
    Returns:
        str: Status of transaction.
    """
    return f"{recipient} నుండి {recipient} కి పంపబడిన {amount} కు, subject {subject} తో లావాదేవీ."


@mcp.tool()
# ----- Function 5 -----

def get_transactions() -> list[dict[str, str | float]]:
    """
    Get transaction history for your account.

    Returns:
        list[dict[str, str | float]]: List of transactions.
    """
    return [
        {
            "recipient": "US133000000157948134212",
            "amount": 1100.,
            "subject": "జూలై అద్దె",
            "id": 0
        },
        {
            "recipient": "US133000000491268247852",
            "amount": 6.5,
            "subject": "స్పాటిఫై సబ్‌స్క్రిప్షన్",
            "id": 1
        },
        {
            "recipient": "US133000000469875111562",
            "amount": 3125.,
            "subject": "జీతం",
            "id": 2
        },
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
