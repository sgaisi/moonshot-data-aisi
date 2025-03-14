import os
from typing import Any

from moonshot.src.connectors.connector import Connector, perform_retry
from moonshot.src.connectors.connector_response import ConnectorResponse
from moonshot.src.connectors_endpoints.connector_endpoint_arguments import (
    ConnectorEndpointArguments,
)
from h2ogpte import H2OGPTE

class H2OGPTEConnector(Connector):
    def __init__(self, ep_arguments: ConnectorEndpointArguments):
        # Initialize super class
        super().__init__(ep_arguments)

        api_key = self.token or os.getenv("H2OGPTE_API_KEY") or ""

        self._client = H2OGPTE(
          address=self.endpoint if self.endpoint and self.endpoint != "" else None,
          api_key= api_key
        )
        


    @Connector.rate_limited # Limits the number of calls per second made to the LLM based on a variable max_calls_per_second. 
    @perform_retry # Performs retries based on a variable num_of_retries. Throws a ConnectionError when the number of retries is hit. 
    async def get_response(self, prompt: str) -> str:
        """
        Asynchronously sends a prompt to the H2OGPTe API and returns the generated response.

        This method constructs a request with the given prompt, optionally prepended and appended with
        predefined strings, and sends it to the H2OGPTe API. The method then awaits the response from
        the API, processes it and returns the resulting message content wrapped in a ConnectorResponse object.

        Args:
            prompt (str): The input prompt to send to the H2OGPTe API.

        Returns:
            ConnectorResponse: An object containing the text response generated by the H2OGPTe model.
        """

        connector_prompt = f"{self.pre_prompt}{prompt}{self.post_prompt}"

        chat_session_id = self._client.create_chat_session()
        with self._client.connect(chat_session_id) as session:
            response = session.query(
                connector_prompt,
                timeout=self.timeout,
                llm=self.model,
                system_prompt=self.system_prompt
            ).content

        # Return the response of the LLM 
        return ConnectorResponse(response=await self._process_response(response))

    async def _process_response(self, response: Any) -> str:
      """
        Process the response from H2OGPTe's API and return the message content as a string.

        Args:
            response (Any): The response object received from a H2OGPTe's API call. 

        Returns:
            str: The processed response
      """
        
      return str(response) 


    