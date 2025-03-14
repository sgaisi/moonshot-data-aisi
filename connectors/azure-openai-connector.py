import os
from typing import Any

from moonshot.src.connectors.connector import Connector, perform_retry
from moonshot.src.connectors.connector_response import ConnectorResponse
from moonshot.src.connectors_endpoints.connector_endpoint_arguments import (
    ConnectorEndpointArguments,
)
from openai import AsyncAzureOpenAI, BadRequestError


class AzureOpenAIConnector(Connector):
    def __init__(self, ep_arguments: ConnectorEndpointArguments):
        # Initialize super class
        super().__init__(ep_arguments)

        # Initialize Azure OpenAI client with additional parameters
        # Select API key from token attribute or environment variable 'AZURE_OPENAI_API_KEY'
        api_key = self.token or os.getenv("AZURE_OPENAI_API_KEY") or ""

        # Select API version from optional parameters or environment variable 'AZURE_OPENAI_VERSION'
        # Default to '2024-02-01' if neither is provided
        api_version = (
            self.optional_params.get("api_version", "")
            or os.getenv("AZURE_OPENAI_VERSION")
            or "2024-02-01"
        )

        # Select API endpoint from endpoint attribute or environment variable 'AZURE_OPENAI_ENDPOINT'
        # Use an empty string if neither is provided
        api_endpoint = self.endpoint or os.getenv("AZURE_OPENAI_ENDPOINT") or ""

        # Configure the AsyncAzureOpenAI client with the specified parameters
        self._client = AsyncAzureOpenAI(
            api_key=api_key,
            # Refer to Azure documentation for API versioning details
            api_version=api_version,
            # Refer to Azure documentation for creating a resource
            azure_endpoint=api_endpoint,
        )

    @Connector.rate_limited
    @perform_retry
    async def get_response(self, prompt: str) -> ConnectorResponse:
        """
        Asynchronously sends a prompt to the OpenAI API and returns the generated response.

        This method constructs a request with the given prompt, optionally prepended and appended with
        predefined strings, and sends it to the OpenAI API. If a system prompt is set, it is included in the
        request. The method then awaits the response from the API, processes it, and returns the resulting message
        content wrapped in a ConnectorResponse object.

        Args:
            prompt (str): The input prompt to send to the OpenAI API.

        Returns:
            ConnectorResponse: An object containing the text response generated by the OpenAI model.
        """
        connector_prompt = f"{self.pre_prompt}{prompt}{self.post_prompt}"
        if self.system_prompt:
            openai_request = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": connector_prompt},
            ]
        else:
            openai_request = [{"role": "user", "content": connector_prompt}]

        # Merge self.optional_params with additional parameters
        new_params = {
            **self.optional_params,
            "model": self.model,
            "messages": openai_request,
            "timeout": self.timeout,
        }

        try:
            response = await self._client.chat.completions.create(**new_params)
            return ConnectorResponse(response=await self._process_response(response))
        except BadRequestError as ex:
            # Azure OpenAI's Content Filter causes HTTP 400 errors when it detects inappropriate content
            if isinstance(ex.body, dict) and "innererror" in ex.body:
                if "code" in ex.body["innererror"]:
                    if (
                        "ResponsibleAIPolicyViolation" in ex.body["innererror"]["code"]
                        and "message" in ex.body
                    ):
                        # For this specific case, we want to continue processing the response as a model
                        # rejection, so we ignore the exception and return a valid looking response
                        return ConnectorResponse(response=ex.body["message"])
            # Otherwise raise the exception
            raise

    async def _process_response(self, response: Any) -> str:
        """
        Process the response from OpenAI's API and return the message content as a string.

        This method processes the response received from OpenAI's API call, specifically targeting
        the chat completion response structure. It extracts the message content from the first choice
        provided in the response, which is expected to contain the relevant information or answer.

        Args:
            response (Any): The response object received from an OpenAI API call. It is expected to
            follow the structure of OpenAI's chat completion response.

        Returns:
            str: A string containing the message content from the first choice in the response. This
            content represents the AI-generated text based on the input prompt.
        """
        return response.choices[0].message.content
