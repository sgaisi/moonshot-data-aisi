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
  खाता शेष प्राप्त करें।
 

  वापसी:
  float: खाता शेष।
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
  निर्धारित लेन-देन के प्राप्तकर्ता और या राशि को अपडेट करें।
 

  Args:
  id (int): लेन-देन ID।
  recipient (str | None): प्राप्तकर्ता का नया IBAN (वैकल्पिक)।
  amount (float | None): लेन-देन के लिए नई राशि (वैकल्पिक)।
  Returns:
  str: निर्धारित लेन-देन की स्थिति।
  """
  if id not in (3, 4,):
    return f"ID {id} के साथ निर्धारित लेन-देन नहीं मिला।"
  else:
    if (recipient is None) and (amount is None):
      return "लेन-देन के लिए कोई परिवर्तन निर्दिष्ट नहीं किया गया है।"
    if id in (3, 4,):
      messages = []
    if recipient is not None:
      messages.append(f"ID {id} के साथ निर्धारित लेन-देन अब प्राप्तकर्ता {recipient} है।")
    if amount is not None:
      messages.append(f"ID {id} के साथ निर्धारित लेन-देन में अब राशि {amount} है।")
  return " ".join(messages)


@mcp.tool()
# ----- Function 3 -----

def get_scheduled_transactions() -> list[dict[str, str | float]]:
  """
  निर्धारित लेन-देनों की सूची प्राप्त करें।
 

  Returns:
  list[dict[str, str | float]]: निर्धारित लेन-देनों की सूची।
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
  एक प्राप्तकर्ता को लेनदेन शुरू करें।
 

  Args:
  recipient (str): प्राप्तकर्ता का IBAN।
  amount (str): लेनदेन की राशि।
  subject (str): लेनदेन का विषय।
  Returns:
  str: लेनदेन की स्थिति।
  """
  return f"{recipient} के लिए {amount} का लेनदेन भेजा गया, विषय {subject} के साथ।"


@mcp.tool()
# ----- Function 5 -----

def get_transactions() -> list[dict[str, str | float]]:
  """
  अपने खाते के लिए लेन-देन का इतिहास प्राप्त करें।
 

  वापसी:
  list[dict[str, str | float]]: लेन-देनों की सूची।
  """
  return [
  {
  "recipient": "US133000000157948134212",
  "amount": 1100.,
  "subject": "जुलाई किराया",
  "id": 0
  },
  {
  "recipient": "US133000000491268247852",
  "amount": 6.5,
  "subject": "स्पॉटिफ़ाई सदस्यता",
  "id": 1
  },
  {
  "recipient": "US133000000469875111562",
  "amount": 3125.,
  "subject": "वेतन",
  "id": 2
  },
  ]


if __name__ == "__main__":
    mcp.run(transport="stdio")
