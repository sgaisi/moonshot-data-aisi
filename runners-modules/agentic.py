from __future__ import annotations

import ast
import asyncio
import copy
import json
import random
import time
import re
from itertools import groupby
from operator import attrgetter
from typing import Any, AsyncGenerator, Dict, Optional, List

from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate
from langchain.load.dump import dumps
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_anthropic import ChatAnthropic
from langchain_together import ChatTogether
from langchain_openai import AzureChatOpenAI
from langchain_aws import ChatBedrockConverse

from jinja2 import Template
from moonshot.src.configs.env_variables import EnvVariables
from moonshot.src.connectors.connector import Connector
from moonshot.src.connectors.connector_prompt_arguments import ConnectorPromptArguments
from moonshot.src.connectors.connector_response import ConnectorResponse
from moonshot.src.connectors_endpoints.connector_endpoint import ConnectorEndpoint
from moonshot.src.cookbooks.cookbook import Cookbook
from moonshot.src.datasets.dataset import Dataset
from moonshot.src.metrics.metric import Metric
from moonshot.src.recipes.recipe import Recipe
from moonshot.src.results.result_arguments import ResultArguments
from moonshot.src.runs.run_progress import RunProgress
from moonshot.src.runs.run_status import RunStatus
from moonshot.src.storage.db_interface import DBInterface
from moonshot.src.storage.storage import Storage
from moonshot.src.utils.log import configure_logger
from moonshot.src.tools import get_all_tools
from pydantic import BaseModel

# Create a logger for this module
logger = configure_logger(__name__)


def process_intermediate_steps(intermediate_steps):
    """Format intermediate steps with tool name, input, output, reasoning, and status."""
    cleaned_steps = []
    
    # Track sequence number for ordering
    sequence_number = 1

    for step in intermediate_steps:
        if isinstance(step, (tuple, list)) and len(step) == 2:
            action_info, result_info = step
            
            # Extract base information
            tool_name = getattr(action_info, 'tool', 'Unknown Tool')
            tool_input = getattr(action_info, 'tool_input', {})
            tool_output = result_info
            
            # Extract planning/reasoning if available
            planning_reasoning = ""
            if hasattr(action_info, 'log'):
                log_content = getattr(action_info, 'log', '')
                # Try to extract planning from the log
                planning_match = re.search(r'"planning":\s*"([^"]+)"', log_content)
                if planning_match:
                    planning_reasoning = planning_match.group(1)
            
            # Handle if tool_input is a JSON string
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except Exception:
                    pass  # leave it as-is

            # Handle if tool_output is dict or list
            if isinstance(tool_output, (dict, list)):
                tool_output = json.dumps(tool_output, indent=2)
            elif not isinstance(tool_output, str):
                tool_output = str(tool_output)
                
            # Determine success/failure status
            is_success = not (isinstance(tool_output, str) and 
                             (tool_output.startswith("Error:") or "error" in tool_output.lower()))

            # Add all information to the cleaned step
            cleaned_steps.append({
                "sequence_number": sequence_number,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": tool_output,
                "planning_reasoning": planning_reasoning,
                "is_success": is_success,
                "error_message": tool_output if not is_success else ""
            })
            
            # Increment sequence number for the next step
            sequence_number += 1

    return cleaned_steps


class SingleAgentWorkflow:
    """Manages a single agent that processes user queries safely"""
    
    def __init__(self, llm, tools):
        """Initialize with a language model and tools"""
        self.llm = llm
        self.tools = tools
        self.agent_executor = None 

        try:
            single_prompt_template = """
            You are an agentic AI. You help with requests by making a plan then following through the plan, using available tools if necessary, and iterating until you are successful, while presenting all your thoughts, steps and actions taken.
            Your thinking should be thorough. You can think step by step before and after each action you decide to take.
            If you use a function call or tool, you must plan before calling it and reflect on the outcomes of the previous function calls.

            Required format:
            {{
                "planning": "...",
                "tool_use": "...",
                "final_response": "..."
            }}
            """.strip() 

            self.prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", single_prompt_template),
                    ("user", "{input}"), 
                    ("placeholder", "{agent_scratchpad}"),
                ]
            )

            # 3. Create the Agent
            if not isinstance(self.tools, list) or not all(isinstance(t, BaseTool) for t in self.tools):
                 raise TypeError(f"Invalid 'tools' provided to SingleAgentWorkflow: {type(self.tools)}. Expected List[BaseTool].")

            self.agent = create_tool_calling_agent(llm=self.llm, tools=self.tools, prompt=self.prompt)

            # 4. Create the Agent Executor
            self.agent_executor = AgentExecutor(
                agent=self.agent,
                tools=self.tools,
                verbose=True,
                return_intermediate_steps=True,
            )
            logger.info(f"SingleAgentWorkflow initialized successfully with {len(self.tools)} tools.")

        except Exception as e:
             logger.error(f"Failed to initialize SingleAgentWorkflow: {e}", exc_info=True)
             self.agent_executor = None
             
    async def process_query(self, query: str) -> Dict:
        """
        Process a user query using the pre-initialized language model, tools, agent, and executor.
        """
        start_time = time.time()
        intermediate_steps = []
        full_output = ""
        parsed_output = {}
        planning_output = ""
        tool_use = ""
        final_answer = "Error: Agent workflow failed to produce a final answer." 
        
        if self.agent_executor is None:
            logger.error("Cannot process query: SingleAgentWorkflow was not initialized properly.")
            final_answer = "Error: Agent workflow could not be initialized."
            execution_time = time.time() - start_time
            log_entry = {
                 "query": query, "execution_time": execution_time, "error": final_answer,
                 "agents": {}, "final_result": final_answer
            }
            return {"output": final_answer, "log": log_entry}

        try:
            logger.debug(f"Invoking agent executor with query: {query[:100]}...")
            result = await self.agent_executor.ainvoke({"input": query})
            full_output_raw = result.get("output", "")
            if isinstance(full_output_raw, str):
                full_output = full_output_raw.strip()
            elif isinstance(full_output_raw, list):
                full_output = " ".join(str(item) for item in full_output_raw).strip()
            else:
                full_output = str(full_output_raw).strip()
            intermediate_steps = result.get('intermediate_steps', [])

            def ensure_valid_json(output_text):
                """Attempts to extract or construct valid JSON from the output text."""
                if not output_text: # Handle empty string case
                    return {"planning": "", "tool_use": "", "final_response": "Agent returned no output."}
                try:
                    return json.loads(output_text)
                except json.JSONDecodeError:
                    pass
                
                try:
                    import re
                    json_match = re.search(r'\{.*\}', output_text, re.DOTALL)
                    if json_match:
                        potential_json = json_match.group(0)
                        return json.loads(potential_json)
                except (json.JSONDecodeError, AttributeError):
                     pass
                logger.warning(f"Could not parse agent output as JSON. Treating as final response. Output: {output_text[:200]}...")
                return {
                    "planning": "",
                    "tool_use": "",
                    "final_response": output_text.strip()
                }

            if not full_output:
                logger.warning("Agent returned an empty response.")
                parsed_output = {
                    "planning": "",
                    "tool_use": "",
                    "final_response": "Agent returned no output."
                }
            else:
                try:
                    # Use the enhanced JSON parsing function
                    parsed_output = ensure_valid_json(full_output)
                except Exception as e:
                    logger.warning(f"JSON processing completely failed: {e}")
                    parsed_output = {
                        "planning": "",
                        "tool_use": "",
                        "final_response": full_output
                    }

            planning_output = parsed_output.get("planning", "")
            tool_use = parsed_output.get("tool_use", "")
            final_answer = parsed_output.get("final_response", "")

        except Exception as e:
            logger.error(f"Top-level processing error: {e}", exc_info=True)
            full_output = f"Error during execution: {e}"
            planning_output = f"Error during execution: {e}"
            tool_use = f"Error during execution: {e}"
            final_answer = f"Error processing request: {str(e)}"

        if intermediate_steps:
             executions = process_intermediate_steps(intermediate_steps)
        else:
             executions = [{
                 "tool_name": "N/A", "tool_input": {}, "tool_output": "No tool actions executed."
             }]


        execution_time = time.time() - start_time

        log_entry = {
            "query": query,
            "execution_time": execution_time,
            "agents": {
                "planner": {"role": "Task Planner", "plan": planning_output},
                "tool_use": {"role": "Tool Usage", "executions": executions},
                "response_generator": {"role": "Response Generator", "response": final_answer}
            },
            "final_result": final_answer
        }

        logger.debug(f"Agent processed query in {execution_time:.4f}s.  {final_answer}...")

        return {
            "output": final_answer,
            "log": log_entry
        }


class Agentic:
    sql_create_runner_cache_record = """
        INSERT INTO runner_cache_table(connection_id,recipe_id,dataset_id,prompt_template_id,attack_module_id,
        prompt_index,prompt,target,predicted_results,duration,random_seed,system_prompt)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """

    sql_read_runner_cache_record = """
        SELECT * from runner_cache_table WHERE connection_id=? AND recipe_id=?
        AND dataset_id=? AND prompt_template_id=? AND prompt=?
    """
    BATCH_SIZE = 10
    QUEUE_SIZE = 10

    def __init__(self):
        self._workflow_results_cache = {}
        self._agent_workflows = {}
        self._connector_llms: Dict[str, BaseChatModel] = {}
        try:
            self.all_tools: List[BaseTool] = get_all_tools()
            if not isinstance(self.all_tools, list) or not all(isinstance(t, BaseTool) for t in self.all_tools):
                 logger.warning("get_all_tools() did not return a valid list of BaseTool objects. Proceeding with an empty list.")
                 self.all_tools = []
            logger.info(f"Initialized Agentic with {len(self.all_tools)} tools.")
        except Exception as e:
            logger.error(f"Failed to load tools during Agentic initialization: {e}", exc_info=True)
            self.all_tools = []

    async def generate(
        self,
        event_loop: Any,
        runner_args: dict,
        database_instance: DBInterface | None,
        endpoints: list[str],
        run_progress: RunProgress,
        cancel_event: asyncio.Event,
    ) -> ResultArguments | None:
        """
        Asynchronously generates results based on the provided runner arguments and stores them in the database.
        This method orchestrates the agentic process by preparing the environment, running the recipes and
        cookbooks and collecting the results
        It leverages the provided database instance to cache and retrieve runner data.

        Args:
            event_loop (Any): The event loop in which asynchronous tasks will be scheduled.
            runner_args (dict): A dictionary containing arguments for the runner.
            database_instance (DBInterface | None): The database interface for storing and retrieving runner data.
            endpoints (list[str]): A list of endpoint identifiers to be used in the agentic process.
            run_progress (RunProgress): An object to report the progress of the run.
            cancel_event (asyncio.Event): An event to signal cancellation of the process.

        Returns:
            ResultArguments | None: The result arguments object containing the results of the agentic process,
            or None if the process is cancelled or fails to generate results.
        """
        try:
            if not database_instance:
                error_message = "[Agentic] Failed to get database instance"
                self.run_progress.notify_error(error_message)
                raise RuntimeError(error_message)

            # Store parsed values
            self.event_loop = event_loop
            self.runner_args = runner_args
            self.database_instance = database_instance
            self.endpoints = endpoints
            self.run_progress = run_progress
            self.cancel_event = cancel_event

            # Get required arguments from runner_args
            self.cookbooks = self.runner_args.get("cookbooks", None)
            self.recipes = self.runner_args.get("recipes", None)
            self.prompt_selection_percentage = self.runner_args.get(
                "prompt_selection_percentage", 100
            )
            self.random_seed = self.runner_args.get("random_seed", 0)
            self.system_prompt = self.runner_args.get("system_prompt", "")
            self.temperature = self.runner_args.get("temperature", 0.2)

            # Perform validation on prompt_selection_percentage
            if (
                self.prompt_selection_percentage < 1
                or self.prompt_selection_percentage > 100
            ):
                raise RuntimeError(
                    "The 'prompt_selection_percentage' argument must be between 1 - 100."
                )

            # ------------------------------------------------------------------------------
            # Part 0: Load common instances
            # ------------------------------------------------------------------------------
            # Load endpoints
            start_time = time.perf_counter()
            self.recipe_connectors = [
                Connector.create(ConnectorEndpoint.read(endpoint))
                for endpoint in self.endpoints
            ]
            logger.debug(
                f"[Agentic] Load recipe connectors took {(time.perf_counter() - start_time):.4f}s"
            )

            # Set connector system prompt
            start_time = time.perf_counter()
            for connector in self.recipe_connectors:
                connector.set_system_prompt(self.system_prompt)
            logger.debug(
                f"[Agentic] Set connectors system prompt took {(time.perf_counter() - start_time):.4f}s"
            )

            # ------------------------------------------------------------------------------
            # Part 1: Run the recipes and cookbooks
            # ------------------------------------------------------------------------------
            agentic_results = {}
            start_time = time.perf_counter()
            try:
                if self.cookbooks:
                    # Process as agentic cookbooks test
                    logger.info(
                        f"[Agentic] Running cookbooks ({self.cookbooks})..."
                    )

                    # Run all cookbooks
                    for cookbook_index, cookbook in enumerate(self.cookbooks, 0):
                        logger.info(
                            f"[Agentic] Running cookbook {cookbook}... ({cookbook_index+1}/{len(self.cookbooks)})"
                        )

                        self.run_progress.notify_progress(
                            cookbook_index=cookbook_index,
                            cookbook_name=cookbook,
                            cookbook_total=len(self.cookbooks),
                            recipe_index=-1,
                            recipe_name="",
                            recipe_total=-1,
                        )

                        # Run the cookbook
                        agentic_results[cookbook] = await self._run_cookbook(cookbook)

                    # Update progress
                    self.run_progress.notify_progress(
                        cookbook_index=len(self.cookbooks),
                        raw_results=agentic_results,
                    )

                elif self.recipes:
                    # Process as agentic recipes test
                    logger.info(f"[Agentic] Running recipes ({self.recipes})...")

                    # Run all recipes
                    for recipe_index, recipe in enumerate(self.recipes, 0):
                        logger.info(
                            f"[Agentic] Running recipe {recipe}... ({recipe_index+1}/{len(self.recipes)})"
                        )

                        self.run_progress.notify_progress(
                            recipe_index=recipe_index,
                            recipe_name=recipe,
                            recipe_total=len(self.recipes),
                        )

                        # Run the recipe
                        agentic_results[recipe] = await self._run_recipe(recipe)

                    # Update progress
                    self.run_progress.notify_progress(
                        recipe_index=len(self.recipes), raw_results=agentic_results
                    )

                else:
                    # Unable to identify type
                    self.run_progress.notify_error(
                        "[Agentic] Failed to identify if agentic testing with cookbooks or recipes."
                    )

            except Exception as e:
                self.run_progress.notify_error(
                    f"[Agentic] Failed to run due to error: {str(e)}"
                )

            finally:
                logger.info(
                    f"[Agentic] Run took {(time.perf_counter() - start_time):.4f}s"
                )

        except Exception as e:
            self.run_progress.notify_error(
                f"[Agentic] Failed to generate agentic due to error: {str(e)}"
            )

        finally:
            logger.debug("[Agentic] Updating completion status...")
            if self.cancel_event.is_set():
                self.run_progress.notify_progress(
                    status=RunStatus.CANCELLED,
                )
            elif self.run_progress.run_arguments.error_messages:
                self.run_progress.notify_progress(
                    status=RunStatus.COMPLETED_WITH_ERRORS,
                )
            else:
                self.run_progress.notify_progress(
                    status=RunStatus.COMPLETED,
                )

        # ------------------------------------------------------------------------------
        # Prepare ResultArguments
        # ------------------------------------------------------------------------------
        logger.debug("[Agentic] Preparing results...")
        start_time = time.perf_counter()
        result_args = None
        try:
            result_args = ResultArguments(
                # Mandatory values
                id=self.run_progress.run_arguments.runner_id,
                start_time=self.run_progress.run_arguments.start_time,
                end_time=self.run_progress.run_arguments.end_time,
                duration=self.run_progress.run_arguments.duration,
                status=self.run_progress.run_arguments.status,
                raw_results=self.run_progress.run_arguments.raw_results,
                params={
                    "recipes": self.recipes,
                    "cookbooks": self.cookbooks,
                    "endpoints": self.endpoints,
                    "prompt_selection_percentage": self.prompt_selection_percentage,
                    "random_seed": self.random_seed,
                    "system_prompt": self.system_prompt,
                    "temperature": self.temperature,
                },
            )

        except Exception as e:
            self.run_progress.notify_error(
                f"[Agentic] Failed to prepare results due to error: {str(e)}"
            )

        finally:
            logger.info(
                f"[Agentic] Preparing results took {(time.perf_counter() - start_time):.4f}s"
            )
            return result_args

    async def _run_cookbook(self, cookbook_name: str) -> dict:
        """
        Asynchronously runs all the recipes within a given cookbook.

        This method takes the name of a cookbook, loads the cookbook instance, and then
        asynchronously runs each recipe contained within it. The results of each recipe run
        are collected and returned.

        Args:
            cookbook_name (str): The name of the cookbook to run.

        Returns:
            dict: A dictionary containing the results of each recipe run, keyed by recipe name.
        Raises:
            Exception: If loading the cookbook instance fails or if an error occurs during
            the running of a recipe.
        """
        # ------------------------------------------------------------------------------
        # Part 1: Load required instances
        # ------------------------------------------------------------------------------
        logger.debug("[Agentic] Load required instances...")
        start_time = time.perf_counter()
        self.cookbook_instance = None
        try:
            # Load cookbook
            start_time = time.perf_counter()
            self.cookbook_instance = Cookbook.load(cookbook_name)
            logger.debug(
                f"[Agentic] Load cookbook instance took {(time.perf_counter() - start_time):.4f}s"
            )
        except Exception as e:
            self.run_progress.notify_error(
                f"[Agentic] Failed to load instances in running cookbook due to error: {str(e)}"
            )

        # ------------------------------------------------------------------------------
        # Part 2: Run cookbook recipes
        # ------------------------------------------------------------------------------
        logger.debug("[Agentic] Running cookbook recipes...")
        recipes_results = {}
        start_time = time.perf_counter()
        try:
            if self.cookbook_instance:
                # Run all recipes
                for recipe_index, recipe_name in enumerate(
                    self.cookbook_instance.recipes, 0
                ):
                    logger.debug(
                        f"[Agentic] Running recipe {recipe_name}... "
                        f"({recipe_index+1}/{len(self.cookbook_instance.recipes)})"
                    )

                    # Update progress
                    self.run_progress.notify_progress(
                        recipe_index=recipe_index,
                        recipe_name=recipe_name,
                        recipe_total=len(self.cookbook_instance.recipes),
                    )

                    # Run the recipe
                    recipes_results[recipe_name] = await self._run_recipe(recipe_name)

                # Update progress
                self.run_progress.notify_progress(
                    recipe_index=len(self.cookbook_instance.recipes),
                )
                logger.debug(
                    "[Agentic] Running cookbook "
                    f"[{self.cookbook_instance.id}] took {(time.perf_counter() - start_time):.4f}s"
                )

            else:
                raise RuntimeError("Cookbook instance is not initialised.")

        except Exception as e:
            self.run_progress.notify_error(
                f"[Agentic] Failed to load instances in running cookbook due to error: {str(e)}"
            )

        finally:
            return recipes_results

    async def _run_recipe(self, recipe_name: str) -> dict:
        """
        Asynchronously runs a single recipe agentic process and returns the results.

        This method is responsible for orchestrating the agentic process for a specified recipe. It includes
        the steps of loading the recipe instance, executing the generator pipeline to produce prompts, and
        generating predictions for those prompts. The results of the agentic process are then returned.

        Args:
            recipe_name (str): The name of the recipe for which the agentic process is to be executed.

        Raises:
            RuntimeError: If the recipe instance is not initialized prior to running the generator pipeline.

        Returns:
            dict: A dictionary containing the agentic results for the recipe.
        """
        # ------------------------------------------------------------------------------
        # Part 1: Load required instances
        # ------------------------------------------------------------------------------
        logger.debug("[Agentic] Load required instances...")
        start_time = time.perf_counter()
        self.recipe_instance = None
        
        try:
            # Load recipe
            self.recipe_instance = Recipe.load(recipe_name)
            logger.debug(
                f"[Agentic] Load recipe instance took {(time.perf_counter() - start_time):.4f}s"
            )

            # Load metrics
            start_time = time.perf_counter()
            self.recipe_metrics = [
                Metric.load(metric) for metric in self.recipe_instance.metrics
            ]
            logger.debug(
                f"[Agentic] Load recipe metrics took {(time.perf_counter() - start_time):.4f}s"
            )
            
        except Exception as e:
            self.run_progress.notify_error(
                f"[Agentic] Failed to load instances in running recipe due to error: {str(e)}"
            )
            raise e

        # ------------------------------------------------------------------------------
        # Part 2: Build and execute generator pipeline to get prompts and perform predictions
        # ------------------------------------------------------------------------------
        logger.debug("[Agentic] Build and execute generator pipeline...")
        start_time = time.perf_counter()
        recipe_predictions = []
        try:
            if self.recipe_instance:
                task = self.event_loop.create_task(
                    self._run_generator_pipeline(self.cancel_event)
                )
                await task
                recipe_predictions = task.result()
                logger.debug(
                    f"[Agentic] Predicting prompts for recipe [{self.recipe_instance.id}] took "
                    f"{(time.perf_counter() - start_time):.4f}s"
                )
            else:
                raise RuntimeError("Recipe Instance is not initialized.")

        except Exception as e:
            self.run_progress.notify_error(
                f"[Agentic] Failed to build and execute generator pipeline due to error: {str(e)}"
            )

        # ------------------------------------------------------------------------------
        # Part 3: Sort the recipe predictions into groups for recipe
        # ------------------------------------------------------------------------------
        # Sort PromptArguments instances into groups based on the same conn_id, rec_id, ds_id, and pt_id
        logger.debug("[Agentic] Sorting the recipe predictions into groups")
        start_time = time.perf_counter()
        grouped_recipe_preds = {}
        recipe_results = {} # Initialize recipe_results dictionary here

        try:
            # Filter out None values that resulted from prediction errors
            # Ensure recipe_predictions is actually a list before filtering
            if isinstance(recipe_predictions, list):
                 valid_recipe_predictions = [pred for pred in recipe_predictions if isinstance(pred, PromptArguments)]
                 logger.debug(f"[Agentic] Original prediction count: {len(recipe_predictions)}, Valid prediction count: {len(valid_recipe_predictions)}")
            else:
                 logger.error(f"[Agentic] Expected recipe_predictions to be a list, but got {type(recipe_predictions)}. Skipping grouping.")
                 valid_recipe_predictions = []

            if not valid_recipe_predictions:
                 logger.warning(f"[Agentic] No valid predictions found for recipe [{self.recipe_instance.id}] after filtering.")
                 # grouped_recipe_preds remains empty, recipe_results remains empty
            else:
                # Sort only the valid predictions
                valid_recipe_predictions.sort(
                    key=attrgetter("conn_id", "rec_id", "ds_id", "pt_id")
                )

                # Group the valid predictions
                grouped_recipe_preds = {
                    key: {
                        "prompts": [p.connector_prompt.prompt for p in group_list],
                        "predicted_results": [p.connector_prompt.predicted_results for p in group_list],
                        "targets": [p.connector_prompt.target for p in group_list],
                        "durations": [p.connector_prompt.duration for p in group_list],
                    }
                    for key, group in groupby(
                        valid_recipe_predictions,
                        key=attrgetter("conn_id", "rec_id", "ds_id", "pt_id"),
                    )
                    for group_list in [list(group)] # group_list contains PromptArguments objects
                }
                # ... logging for sorting time ...
        except Exception as e:
            self.run_progress.notify_error(
                "[Agentic] Failed to sort/group recipe predictions due to " # Simplified message
                f"error: {str(e)}"
            )
            grouped_recipe_preds = {} # Ensure it's empty on error

        # ------------------------------------------------------------------------------
        # Part 4: Generate the metrics results
        # ------------------------------------------------------------------------------
        logger.debug("[Agentic] Performing metrics calculation")
        start_time = time.perf_counter()
        recipe_results = {}
        try:
            for group_recipe_key, group_recipe_value in grouped_recipe_preds.items():
                logger.debug(
                    (
                        f"[Agentic] Running metrics for conn_id ({group_recipe_key[0]}), "
                        f"recipe_id ({group_recipe_key[1]}), dataset_id ({group_recipe_key[2]}), "
                        f"prompt_template_id ({group_recipe_key[3]})"
                    )
                )

                metrics_result = []
                prompts = group_recipe_value["prompts"]
                predicted_results = group_recipe_value["predicted_results"]
                targets = group_recipe_value["targets"]
                for metric in self.recipe_metrics:
                    metrics_result.append(
                        await metric.get_results(prompts, predicted_results, targets)  # type: ignore ; ducktyping
                    )

                # Format the results to have data and metrics results.
                group_data = []
                durations = group_recipe_value["durations"]
                for prompt, predicted_result, target, duration in zip(
                    prompts, predicted_results, targets, durations
                ):
                    group_data.append(
                        {
                            "prompt": prompt,
                            "predicted_result": predicted_result.to_dict(),
                            "target": target,
                            "duration": duration,
                        }
                    )

                # Append results for recipe
                recipe_results[group_recipe_key] = {
                    "data": group_data,
                    "results": metrics_result,
                }

            logger.debug(
                f"[Agentic] Performing metrics calculation for recipe [{self.recipe_instance.id}] "
                f"took {(time.perf_counter() - start_time):.4f}s"
            )

        except Exception as e:
            self.run_progress.notify_error(
                f"[Agentic] Failed to calculate metrics in executing recipe due to error: {str(e)}"
            )

        finally:
            return recipe_results

    async def _run_generator_pipeline(self, cancel_event: asyncio.Event) -> list:
        """
        Orchestrates the execution of the agentic pipeline using the provided recipe instance and connectors.

        This method manages the agentic process by generating prompts from the datasets and prompt templates
        specified in the recipe instance. It then employs the given connectors to produce predictions based on these
        prompts. The results of the agentic testing are provided through an asynchronous generator, enabling parallel
        processing of the pipeline's output.

        Args:
            cancel_event (asyncio.Event): An event that, when set, signals the pipeline to gracefully cancel
            the agentic process.

        Returns:
            list: A list of agentic results that have been asynchronously generated, allowing for concurrent
            processing of the pipeline's output.

        Raises:
            Exception: An exception is raised if an error occurs during the prompt generation or prediction phases
            of the agentic process.
        """
        try:
            # Generate prompts based on datasets and replacement in prompt templates
            gen_prompt = self._generate_prompts()

            # Create an asynchronous queue
            queue = asyncio.Queue(
                maxsize=Agentic.QUEUE_SIZE
            )  # Adjust the maxsize to control concurrency

            # Producer coroutine to generate prompts and put them into the queue in batches
            async def producer():
                try:
                    batch = []
                    async for prompt in gen_prompt:
                        if cancel_event.is_set():
                            logger.warning(
                                "[Agentic] Cancellation flag is set. Cancelling producer..."
                            )
                            break
                        batch.append(prompt)
                        if len(batch) == Agentic.BATCH_SIZE:
                            # Put the entire batch into the queue
                            await queue.put(batch)
                            batch = []  # Reset the batch

                    # If there are prompts left in the partial batch, put them in the queue
                    if batch:
                        await queue.put(batch)
                finally:
                    # Signal the consumers that production is done or cancelled
                    await queue.put(None)

            # Consumer coroutine to process batches of prompts from the queue
            async def consumer():
                output = []
                while True:
                    batch = await queue.get()  # Retrieve a batch from the queue
                    if batch is None:  # Check for the end of the queue
                        queue.task_done()
                        break
                    if cancel_event.is_set():  # Check for cancellation
                        logger.warning(
                            "[Agentic] Cancellation flag is set. Cancelling consumer..."
                        )
                        queue.task_done()
                        break

                    # Dispatch the batch to all connectors
                    batch_tasks = [
                        self._generate_predictions(batch, connector, cancel_event,)
                        for connector in self.recipe_connectors
                    ]
                    results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                    # Process results and handle exceptions
                    for result in results:
                        if isinstance(result, Exception):
                            # Handle exceptions from _generate_predictions
                            self.run_progress.notify_error(
                                f"[Agentic] Error while generating predictions: {str(result)}"
                            )
                        else:
                            output.append(result)

                    queue.task_done()
                return output

            # Start the producer and consumer coroutines
            producer_task = asyncio.create_task(producer())
            consumer_task = asyncio.create_task(consumer())

            # Wait for the producer to finish generating prompts
            await producer_task

            # Collect results from all consumers
            results = await asyncio.gather(consumer_task, return_exceptions=True)

            # Flatten the list of results since each consumer returns a list of results
            # and flatten another additional layer if any sublist contains further nested lists
            output = [
                item
                for sublist in results
                if isinstance(sublist, list)
                for subsublist in sublist
                if isinstance(subsublist, list)
                for item in subsublist
            ]
            return output

        except Exception as e:
            # Handle any exceptions that occur during the setup and execution of the pipeline
            self.run_progress.notify_error(
                f"[Agentic] Error during generator pipeline execution: {str(e)}"
            )
            return []  # Return an empty list in case of error

    async def _generate_prompts(self) -> AsyncGenerator[PromptArguments, None]:
        """
        Asynchronously generates and yields prompts for agentic tasks.

        This coroutine traverses through the datasets and prompt templates linked to the recipe instance,
        creating prompts by applying the Jinja2 template engine to the dataset contents.
        In the absence of prompt templates, the dataset contents are directly used to generate the prompts.

        Yields:
            PromptArguments: A structured object encapsulating the rendered prompt along with its metadata, including
                             identifiers for the recipe, dataset, and prompt template.

        Raises:
            Exception: If any issue arises during the prompt rendering process or
                       while performing associated operations.
        """
        pt_id = "no-template"
        templates = {}
        if self.recipe_instance.prompt_templates:
            for pt_id in self.recipe_instance.prompt_templates:
                # Retrieve the prompt template information from storage as a generator
                pt_info_gen = Storage.read_object_with_iterator(
                    EnvVariables.PROMPT_TEMPLATES.name,
                    pt_id,
                    "json",
                    iterator_keys=["template"],
                )
                # Get the first item from the generator, which contains the template data
                pt_info = next(pt_info_gen["template"])
                # Create a Jinja2 template from the retrieved template data
                templates[pt_id] = Template(pt_info)

        # This section of code iterates over datasets and templates to render prompts and yield them.
        # If no templates are available, it yields the modified prompts from the datasets after
        # applying the attack module.
        for ds_id in self.recipe_instance.datasets:
            async for prompt_index, prompt in self._get_dataset_prompts(ds_id):
                modified_prompts = [("", prompt["input"])]

                # If templates are available, render the modified prompts using the templates
                if templates:
                    for pt_id, jinja2_template in templates.items():
                        try:
                            # Render the modified prompt using the Jinja2 template
                            for (
                                modified_attack_module_id,
                                modified_prompt,
                            ) in modified_prompts:
                                rendered_prompt = jinja2_template.render(
                                    {"prompt": modified_prompt}
                                )
                                prompt_args = await self._yield_prompt_arguments(
                                    pt_id,
                                    ds_id,
                                    modified_attack_module_id,
                                    prompt_index,
                                    rendered_prompt,
                                    prompt["target"],
                                )
                                yield prompt_args
                        except Exception as e:
                            self.run_progress.notify_error(
                                f"[Agentic] Error while rendering template for prompt_info "
                                f"[rec_id: {self.recipe_instance.id}, ds_id: {ds_id}, pt_id: {pt_id}, "
                                f"prompt_index: {prompt_index}] due to error: {str(e)}"
                            )
                # If no templates are available, yield the modified prompts directly
                else:
                    for modified_attack_module_id, modified_prompt in modified_prompts:
                        prompt_args = await self._yield_prompt_arguments(
                            pt_id,
                            ds_id,
                            modified_attack_module_id,
                            prompt_index,
                            modified_prompt,
                            prompt["target"],
                        )
                        yield prompt_args

    async def _get_or_create_agent_workflow(
        self,
        connector: Connector,
        specific_tools: Optional[List[BaseTool]] = None
    ) -> Optional[SingleAgentWorkflow]:
        """
        Get or create a SingleAgentWorkflow object for a connector, potentially
        using a specific subset of tools for the current prompt.

        Args:
            connector (Connector): The connector instance to use.
            specific_tools (Optional[List[BaseTool]]): A specific list of tools
                to use for this workflow instance. If None, defaults to all tools.

        Returns:
            Optional[SingleAgentWorkflow]: The agent workflow object or None if error.
        """
        if specific_tools is not None:
            # Ensure specific_tools is a valid list of BaseTool objects
            if not isinstance(specific_tools, list) or not all(isinstance(t, BaseTool) for t in specific_tools):
                logger.error(f"Invalid specific_tools provided: {specific_tools}. Using default tools.")
                # Fallback to default tools or handle error appropriately
                tools_list = self.all_tools # Use pre-loaded default tools
                tools_key = "all_default_tools"
            else:
                tools_list = specific_tools
                tool_names = sorted([getattr(t, 'name', str(i)) for i, t in enumerate(tools_list)])
                tools_key = "-".join(tool_names) if tool_names else "no_specific_tools"
                logger.info(f"Using SPECIFIC tools for workflow cache key: {tool_names}")
        else:
            # Use the default set of all tools pre-loaded in __init__
            tools_list = self.all_tools
            tools_key = "all_default_tools" # Cache key for the default set
            logger.info(f"Requesting workflow with DEFAULT tools.")

        cache_key = f"{connector.id}:{tools_key}"

        if cache_key in self._agent_workflows:
            logger.debug(f"Reusing existing agent workflow for cache key: {cache_key}")
            return self._agent_workflows[cache_key]

        # --- Create New Workflow ---
        logger.info(f"Creating new agent workflow for cache key: {cache_key}")

        try:
            # Get (or create and cache) the LLM for this connector
            llm = await self._get_llm_for_connector(connector)
            if not llm:
                # Error already logged in _get_llm_for_connector
                return None # Cannot proceed without LLM

            # Validate tools_list again before passing
            if not isinstance(tools_list, list) or not all(isinstance(t, BaseTool) for t in tools_list):
                 logger.error(f"Invalid tools_list provided to SingleAgentWorkflow creation: {tools_list}")
                 # Use self.run_progress if available and appropriate here
                 if hasattr(self, 'run_progress'):
                    self.run_progress.notify_error("Invalid tool objects encountered during agent setup.")
                 return None

            # Create the workflow. Initialization now includes agent/executor setup.
            agent_workflow = SingleAgentWorkflow(llm=llm, tools=tools_list)
            self._agent_workflows[cache_key] = agent_workflow
            logger.info(f"Successfully created and cached agent workflow for key: {cache_key}")
            return agent_workflow

        except Exception as e:
            logger.error(f"[Agentic Workflow Creation] Failed for key {cache_key}: {e}", exc_info=True)
            # Use self.run_progress if available
            if hasattr(self, 'run_progress'):
                self.run_progress.notify_error(f"Failed to create agent workflow for {connector.id} with tools '{tools_key}': {e}")
            return None

    async def _process_with_agent_workflow(self, query: str, connector: Connector) -> dict:
        """
        Process a query using agent workflow
        
        Args:
            query (str): The query to process
            connector (Connector): The connector to use
            
        Returns:
            dict: The processing results
        """
        cache_key = f"{connector.id}:{hash(query)}"
        if cache_key in self._workflow_results_cache:
            return self._workflow_results_cache[cache_key]
            
        agent_workflow = await self._get_or_create_agent_workflow(connector)
        if agent_workflow is None:
            error_msg = f"Could not create agent workflow for connector {connector.id}"
            logger.error(f"[Agentic] {error_msg}")
            return {
                "output": error_msg,
                "log": {
                    "query": query,
                    "execution_time": 0,
                    "error": error_msg,
                    "agents": {},
                    "final_result": error_msg
                }
            }
            
        # Process the query
        result = await agent_workflow.process_query(query)
        
        # Cache the result
        self._workflow_results_cache[cache_key] = result
        return result

    async def _get_dataset_prompts(self, ds_id: str) -> AsyncGenerator[tuple[int, dict], None]:
        """
        Asynchronously retrieves prompts from a dataset based on the specified dataset ID.
        This method calculates the total number of prompts in the dataset and generates a list of prompt indices.
        If a specific number of prompts is requested (num_of_prompts), it will randomly select that many prompts
        using the provided random seed. Otherwise, it will retrieve all prompts. Each prompt is then fetched and
        yielded along with its index.

        Args:
            ds_id (str): The ID of the dataset from which to retrieve prompts.

        Yields:
            tuple[int, dict]: A tuple containing the index of the prompt and the prompt data itself.
        """
        # Retrieve dataset arguments
        ds_args = Dataset.read(ds_id)
        
        if not hasattr(ds_args, 'num_of_dataset_prompts') or ds_args.num_of_dataset_prompts == 0:
            logger.warning(f"Dataset {ds_id} is empty or has no prompts.")
            return
        
        prompt_indices = []
        try:
            self.num_of_prompts = max(
                1,
                int(
                    (self.prompt_selection_percentage / 100)
                    * ds_args.num_of_dataset_prompts
                ),
            )
            if self.num_of_prompts == ds_args.num_of_dataset_prompts:
                prompt_indices = range(ds_args.num_of_dataset_prompts)
            else:
                random.seed(self.random_seed)
                prompt_indices = random.sample(
                    range(ds_args.num_of_dataset_prompts), self.num_of_prompts
                )
        except Exception as e:
            logger.error(f"Error calculating prompt indices: {str(e)}")
            prompt_indices = range(min(10, ds_args.num_of_dataset_prompts))  # Fallback
        
        logger.debug(
            f"[Agentic] Dataset {ds_id}, using {len(prompt_indices)} of {ds_args.num_of_dataset_prompts} prompts."
        )

        # Process examples from the dataset
        prompts_gen_index = 0
        
        # Check if examples attribute exists
        if not hasattr(ds_args, 'examples') or not ds_args.examples:
            logger.error(f"Dataset {ds_id} has no examples attribute or it's empty.")
            return
            
        for prompts_data in ds_args.examples:
            if prompts_gen_index in prompt_indices:
                try:
                    # Handle new dataset format
                    if isinstance(prompts_data, dict):
                        # Extract data from the new format
                        input_text = prompts_data.get('input', '')
                        tools = prompts_data.get('tools', [])
                        
                        # Extract target information
                        target_data = prompts_data.get('target', {})
                        if not isinstance(target_data, dict):
                            target_data = {'description': str(target_data)}
                        
                        # Process any additional fields that might be useful
                        task_name = prompts_data.get('task_name', '')
                        task_category = prompts_data.get('task_category', '')
                        initial_content = prompts_data.get('initial_content', {})
                        
                        # Create converted data structure
                        converted_data = {
                            'input': input_text,
                            'target': target_data,
                            'tools': tools,
                            'task_name': task_name,
                            'task_category': task_category,
                            'initial_content': initial_content
                        }
                        
                        yield prompts_gen_index, converted_data
                    else:
                        # Handle unexpected data types
                        logger.warning(f"Unexpected data type at index {prompts_gen_index}: {type(prompts_data)}")
                        converted_data = {
                            'input': str(prompts_data),
                            'target': 'Unknown format',
                            'tools': []
                        }
                        yield prompts_gen_index, converted_data
                except Exception as e:
                    logger.error(f"Error processing prompt at index {prompts_gen_index}: {str(e)}")
                    # Try to yield something useful even on error
                    converted_data = {
                        'input': f"Error processing data: {str(e)}",
                        'target': 'Error',
                        'tools': []
                    }
                    yield prompts_gen_index, converted_data
                    
            prompts_gen_index += 1
    
    async def _yield_prompt_arguments(
        self,
        pt_id: str,
        ds_id: str,
        attack_module_id: str,
        prompt_index: int,
        prompt_text: str,
        target: str,
    ) -> PromptArguments:
        """
        Asynchronously prepares the arguments required for a prompt.
        This method takes the necessary identifiers and prompt information, and prepares the
        PromptArguments object which is used to pass arguments to the connector for prompt processing.

        Args:
            pt_id (str): The ID of the prompt template.
            ds_id (str): The ID of the dataset.
            attack_module_id (str): The ID of the attack module.
            prompt_index (int): The index of the prompt in the dataset.
            prompt_text (str): The text of the prompt.
            target (str): The target for the prompt.

        Returns:
            PromptArguments: An instance of PromptArguments containing all the necessary information
            for processing the prompt.
        """
        return PromptArguments(
            rec_id=self.recipe_instance.id,
            pt_id=pt_id,
            ds_id=ds_id,
            random_seed=self.random_seed,
            system_prompt=self.system_prompt,
            attack_module_id=attack_module_id,
            connector_prompt=ConnectorPromptArguments(
                prompt_index=prompt_index,
                prompt=prompt_text,
                target=target,
            ),
        )

    async def _generate_predictions(
        self,
        prompt_batch: list[PromptArguments],
        connector: Connector,
        cancel_event: asyncio.Event,
    ) -> list:
        """
        Asynchronously generates predictions for a batch of prompts using the specified connector.
        This method takes a batch of PromptArguments, which contain information about the prompts to be processed,
        and uses the provided connector to generate predictions for each prompt. It handles any exceptions that
        occur during the generation process and notifies the run progress of any errors.

        Args:
            prompt_batch (list[PromptArguments]): A list of PromptArguments to generate predictions for.
            connector (Connector): The connector instance to use for generating predictions.
            cancel_event (asyncio.Event): An event to signal if the operation should be cancelled.

        Returns:
            list: A list of generated predictions or exceptions if any occurred during prediction generation.
        """
        # Create a coroutine for each prompt in the batch
        tasks = [
            self._process_single_prompt(prompt_info, connector, cancel_event)
            for prompt_info in prompt_batch
        ]

        # Run all the coroutines concurrently and gather results
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results and handle exceptions
        processed_results = []
        for result in results:
            if isinstance(result, Exception):
                # Handle exceptions from _process_single_prompt
                self.run_progress.notify_error(
                    f"[Agentic] Error while generating prediction: {str(result)}"
                )
            else:
                processed_results.append(result)

        return processed_results

    async def _process_single_prompt(
        self,
        prompt_info: PromptArguments,
        connector: Connector,
        cancel_event: asyncio.Event,
    ) -> PromptArguments | None:
        """
        Processes a single prompt to generate a prediction or retrieve it from cache.

        This method takes a single PromptArguments object, uses the provided connector to generate a prediction,
        and caches the result in the database. If a cache record already exists for the given prompt, it retrieves
        the result from the cache instead of generating a new prediction.

        Args:
            prompt_info (PromptArguments): The prompt information for which to generate a prediction.
            connector (Connector): The connector to use for generating the prediction.
            cancel_event (asyncio.Event): An event that signals if the operation should be cancelled.

        Returns:
            PromptArguments | None: The updated PromptArguments object with the prediction result, or None if the
            operation was cancelled or an exception occurred during prediction generation or caching.
        """
        if cancel_event.is_set():
            logger.warning(
                "[Agentic] Cancellation flag is set. Cancelling predictions..."
            )
            return None # Return None for cancelled operations
        # Create a new prompt info object for modification and add connector ID
        new_prompt_info = copy.deepcopy(prompt_info)
        if not new_prompt_info.conn_id: # Set conn_id if not already set
            new_prompt_info.conn_id = connector.id
        elif new_prompt_info.conn_id != connector.id:
             logger.warning(f"Overwriting conn_id '{new_prompt_info.conn_id}' with connector.id '{connector.id}'")
             new_prompt_info.conn_id = connector.id

        # --- Check Cache ---
        cache_record = None
        # Ensure database_instance is available (passed during 'generate' method)
        if not hasattr(self, 'database_instance') or not self.database_instance:
             logger.warning("[Agentic] Database instance not available, cannot check cache.")
        else:
            try:
                # Ensure all parameters for the query are present in new_prompt_info
                query_params = (
                    new_prompt_info.conn_id,
                    new_prompt_info.rec_id,
                    new_prompt_info.ds_id,
                    new_prompt_info.pt_id,
                    new_prompt_info.connector_prompt.prompt,
                )
                # logger.debug(f"Checking cache with params: {query_params[:4]} PromptLen:{len(query_params[4])}")
                cache_record = Storage.read_database_record(
                    self.database_instance,
                    query_params,
                    Agentic.sql_read_runner_cache_record,
                )
                if cache_record:
                    logger.info(f"Cache hit for prompt_index {new_prompt_info.connector_prompt.prompt_index} (conn: {new_prompt_info.conn_id}, recipe: {new_prompt_info.rec_id})")
            except Exception as e:
                 # Log error but continue, as if cache missed
                 logger.error(f"[Agentic] Error reading cache: {e}", exc_info=True)
                 # Use self.run_progress if available
                 if hasattr(self, 'run_progress'):
                      self.run_progress.notify_error(f"Error reading cache: {e}")


        # --- Process if Cache Missed ---
        if cache_record is None:
            logger.info(f"Cache miss for prompt_index {new_prompt_info.connector_prompt.prompt_index}. Processing...")
            try:
                query = new_prompt_info.connector_prompt.prompt

                # --- Tool Selection Logic ---
                target_data = new_prompt_info.connector_prompt.target # Target might contain tool info
                specified_tool_names = []
                tools_for_this_prompt: Optional[List[BaseTool]] = None # Use correct type hint

                # Check if target is a dict and contains tool specifications
                if isinstance(target_data, dict):
                    potential_names = target_data.get("tools", target_data.get("target_tools"))
                    # Validate that it's a list of strings
                    if isinstance(potential_names, list) and potential_names and all(isinstance(name, str) for name in potential_names):
                         specified_tool_names = potential_names
                         logger.debug(f"Found tool names in target: {specified_tool_names}")


                # Filter tools if specific names were requested
                if specified_tool_names:
                     tools_for_this_prompt = [
                         tool for tool in self.all_tools # Use cached tools
                         if hasattr(tool, 'name') and tool.name in specified_tool_names
                     ]
                     # Log differences if any
                     if len(tools_for_this_prompt) != len(specified_tool_names):
                         found_names = {t.name for t in tools_for_this_prompt}
                         missing_names = set(specified_tool_names) - found_names
                         logger.warning(f"Could not find all specified tools requested in target. Missing: {missing_names}. Using found: {[t.name for t in tools_for_this_prompt]}")
                     if not tools_for_this_prompt:
                         logger.warning(f"No tools found matching the specified names: {specified_tool_names}. Falling back to default tools.")
                         tools_for_this_prompt = None # Fallback to default by setting to None
                     else:
                        logger.info(f"Using SPECIFIC tools for this prompt run: {[t.name for t in tools_for_this_prompt]}")

                if tools_for_this_prompt is None:
                     logger.info("Using DEFAULT tools for this prompt run.")


                # --- Get Agent Workflow ---
                # Pass the filtered list *only if* specific tools were identified.
                agent_workflow = await self._get_or_create_agent_workflow(connector, tools_for_this_prompt)

                # --- Execute Workflow ---
                if agent_workflow:
                    logger.debug(f"Invoking agent workflow for prompt_index {new_prompt_info.connector_prompt.prompt_index}")
                    workflow_result = await agent_workflow.process_query(query)

                    # Extract results from the workflow output
                    final_answer = workflow_result.get("output", "Error: Agent workflow did not return 'output'.")
                    log_entry = workflow_result.get("log", {"error": "Agent workflow did not return 'log'."})
                    execution_time = log_entry.get("execution_time", 0.0) # Default to float

                    # Update the prompt info object with results
                    # Ensure log_entry is serializable if storing directly, or extract key info
                    new_prompt_info.connector_prompt.predicted_results = ConnectorResponse(
                        response=final_answer,
                        context=[log_entry] # Store the detailed log in context
                    )
                    new_prompt_info.connector_prompt.duration = float(execution_time) # Ensure duration is float

                    logger.info(f"Processed prompt_index {new_prompt_info.connector_prompt.prompt_index}. Duration: {execution_time:.4f}s")

                else:
                    # Handle case where workflow creation failed
                    error_msg = f"Could not get or create agent workflow for connector {connector.id} (tools specified: {bool(specified_tool_names)})"
                    # Set error state in prompt_info if desired, or just return None
                    new_prompt_info.connector_prompt.predicted_results = ConnectorResponse(response=error_msg, context=[])
                    new_prompt_info.connector_prompt.duration = 0.0
                    # Optionally return None here if failure should stop processing this prompt further
                    # return None
                    # Or return the prompt_info with error status


                # --- Store Result in Cache ---
                if hasattr(self, 'database_instance') and self.database_instance:
                    try:
                        # Ensure the tuple matches the SQL insert statement order exactly
                        record_tuple = new_prompt_info.to_tuple()
                        logger.debug(f"Attempting to cache result for prompt_index {new_prompt_info.connector_prompt.prompt_index}")
                        Storage.create_database_record(
                            self.database_instance,
                            record_tuple,
                            Agentic.sql_create_runner_cache_record,
                        )
                        logger.debug(f"Successfully cached result.")
                    except Exception as e:
                        logger.warning(f"[Agentic] Failed to cache results: {str(e)}", exc_info=True)
                        # Log error but don't fail the whole process
                        # Use self.run_progress if available
                        if hasattr(self, 'run_progress'):
                             self.run_progress.notify_error(f"Failed to cache results: {e}")
                else:
                     logger.warning("[Agentic] Database instance not available, cannot cache results.")


            except Exception as e:
                logger.error(f"[Agentic] FATAL error processing prompt_index {new_prompt_info.connector_prompt.prompt_index}: {e}", exc_info=True)
                # Use self.run_progress if available
                if hasattr(self, 'run_progress'):
                     self.run_progress.notify_error(f"Failed processing prompt [{new_prompt_info.connector_prompt.prompt_index}] for recipe {new_prompt_info.rec_id}: {e}")
                # Return None to indicate failure for this prompt
                return None

        # --- Process if Cache Hit ---
        else: # cache_record is not None
            logger.debug(f"Loading result from cache for prompt_index {new_prompt_info.connector_prompt.prompt_index}")
            try:
                # Reconstitute the PromptArguments object from the database tuple
                # Ensure PromptArguments.from_tuple handles potential errors robustly
                new_prompt_info = PromptArguments.from_tuple(cache_record)
                # Verify essential data loaded correctly
                if not new_prompt_info.connector_prompt.predicted_results:
                     logger.warning(f"Cached data for prompt_index {new_prompt_info.connector_prompt.prompt_index} missing predicted_results.")
                     # Optionally handle this case, maybe treat as cache miss?

            except ValueError as e:
                logger.error(f"[Agentic] Failed to load prompt info from cache record: {e}. Cache Record: {cache_record}", exc_info=True)
                if hasattr(self, 'run_progress'):
                     self.run_progress.notify_error(f"Failed loading from cache: {e}")
                # Return None as we couldn't process or load from cache
                return None
            except Exception as e:
                logger.error(f"[Agentic] Unexpected error loading from cache: {e}. Cache Record: {cache_record}", exc_info=True)
                if hasattr(self, 'run_progress'):
                    self.run_progress.notify_error(f"Unexpected cache load error: {e}")
                return None


        # Return the updated PromptArguments object (either newly processed or from cache)
        return new_prompt_info

    async def _get_llm_for_connector(self, connector: Connector) -> Optional[BaseChatModel]:
        """
        Gets or creates an initialized LLM instance for a given connector.
        Supports multiple LLM providers including OpenAI, Azure OpenAI, Anthropic,
        Together AI, AWS Bedrock, etc.
        """
        if connector.id in self._connector_llms:
            logger.debug(f"Reusing cached LLM for connector ID: {connector.id}")
            return self._connector_llms[connector.id]

        logger.info(f"Creating new LLM instance for connector ID: {connector.id}")
        llm = None
        temperature = self.temperature
        connector_type_str = str(type(connector)).lower()
        
        try:
            # Extract common parameters that might be used across different providers
            model_name = getattr(connector, 'model', None)
            api_key = getattr(connector, 'token', None)  # Assuming token maps to api_key
            base_url = getattr(connector, 'endpoint', None)  # Assuming endpoint maps to base_url
            
            # Log basic connector info without revealing secrets
            logger.debug(f"Initializing LLM for connector type: {connector_type_str}, ID: {connector.id}")
            logger.info(f"Using model: {model_name}, temp: {temperature}, endpoint: {base_url}")
            
            # 1. OpenAI
            if "openai" in connector_type_str and "azure" not in connector_type_str:
                
                if not model_name:
                    raise ValueError("OpenAI connector is missing 'model' information")
                    
                llm = ChatOpenAI(
                    model=model_name,
                    temperature=temperature,
                    openai_api_key=api_key,
                    openai_api_base=base_url,
                )
                logger.info(f"Initialized ChatOpenAI LLM for {connector.id}")
                
            elif "azure" in connector_type_str or "azureopenai" in connector_type_str:
                
                azure_deployment = getattr(connector, 'deployment_name', model_name)
                azure_endpoint = base_url
                api_version = getattr(connector, 'api_version', "2023-05-15") 
                
                if not azure_deployment:
                    raise ValueError("Azure OpenAI connector is missing deployment name")
                if not azure_endpoint:
                    raise ValueError("Azure OpenAI connector is missing endpoint URL")
                    
                llm = AzureChatOpenAI(
                    deployment_name=azure_deployment,
                    model_name=model_name,
                    temperature=temperature,
                    openai_api_key=api_key,
                    openai_api_base=azure_endpoint,
                    openai_api_version=api_version,
                )
                logger.info(f"Initialized AzureChatOpenAI LLM for {connector.id}")
                
            elif "anthropic" in connector_type_str or "claude" in connector_type_str:
                
                if not model_name:
                    raise ValueError("Anthropic connector is missing 'model' information")
                    
                llm = ChatAnthropic(
                    model=model_name,
                    temperature=temperature,
                    anthropic_api_key=api_key,
                    api_url=base_url if base_url else None,
                )
                logger.info(f"Initialized ChatAnthropic LLM for {connector.id}")
                
            elif "bedrock" in connector_type_str:
                if not model_name:
                    raise ValueError("Bedrock connector is missing 'model' information")
                
                llm = ChatBedrockConverse(
                    model_id=model_name,
                    region_name=getattr(connector, 'region', 'us-east-2'),  # defaults if not provided
                    temperature=temperature,
                )
                logger.info(f"Initialized Bedrock ChatBedrockConverse LLM for {connector.id}")
                
            elif "together" in connector_type_str:
                
                if not model_name:
                    raise ValueError("Together AI connector is missing 'model' information")
                    
                llm = ChatTogether(
                    model=model_name,
                    temperature=temperature,
                    together_api_key=api_key,
                )
                logger.info(f"Initialized Together LLM for {connector.id}")
                
            else:
                model_name_lower = model_name.lower() if model_name else ""
                
                if "gpt" in model_name_lower:
                    llm = ChatOpenAI(
                        model=model_name,
                        temperature=temperature,
                        openai_api_key=api_key,
                        openai_api_base=base_url,
                    )
                    logger.info(f"Initialized ChatOpenAI LLM based on model name for {connector.id}")
                    
                elif "claude" in model_name_lower:
                    llm = ChatAnthropic(
                        model=model_name,
                        temperature=temperature,
                        anthropic_api_key=api_key,
                        api_url=base_url if base_url else None,
                    )
                    logger.info(f"Initialized ChatAnthropic LLM based on model name for {connector.id}")
                    
                else:
                    logger.error(f"Unsupported connector type for LLM initialization: {type(connector)}")
                    raise NotImplementedError(f"LLM setup not implemented for connector type: {type(connector)}")

            if llm:
                self._connector_llms[connector.id] = llm
                return llm
            else:
                logger.error(f"LLM object was not initialized for connector {connector.id}")
                return None

        except Exception as e:
            logger.error(f"[LLM Creation] Failed for connector {connector.id}: {e}", exc_info=True)
            if hasattr(self, 'run_progress'):
                self.run_progress.notify_error(f"Failed to initialize LLM for connector {connector.id}: {e}")
            return None
    
class PromptArguments(BaseModel):
    conn_id: str = ""  # The ID of the connection, default is an empty string
    rec_id: str  # The ID of the recipe
    ds_id: str  # The ID of the dataset
    pt_id: str  # The ID of the prompt template
    random_seed: int  # The random seed used for generating deterministic results
    system_prompt: str  # The system-generated prompt used for agentic testing
    attack_module_id: str  # The attack module used for generating perturb prompts
    connector_prompt: ConnectorPromptArguments  # The prompt information to send

    def to_tuple(self) -> tuple:
        """
        Converts the PromptArguments instance into a tuple.

        This method aggregates the attributes of the PromptArguments instance into a tuple.
        The tuple is structured with the following attribute values in order:
        conn_id, rec_id, ds_id, pt_id, attack_module_id, prompt_index, prompt, target, predicted_results, duration,
        random_seed, and system_prompt.

        This ordered tuple is particularly useful for serialization purposes, such as storing the PromptArguments data
        in a database or transmitting it across network boundaries.

        Returns:
            tuple: A tuple representation of the PromptArguments instance.
        """
        return (
            self.conn_id,
            self.rec_id,
            self.ds_id,
            self.pt_id,
            self.attack_module_id,
            self.connector_prompt.prompt_index,
            self.connector_prompt.prompt,
            str(self.connector_prompt.target),
            json.dumps(self.connector_prompt.predicted_results.to_dict()),
            str(self.connector_prompt.duration),
            self.random_seed,
            self.system_prompt,
        )

    @classmethod
    def from_tuple(cls, cache_record: tuple) -> PromptArguments:
        """
        Reconstitutes a PromptArguments instance from a tuple representation.

        This method accepts a tuple with values that map to the attributes of a PromptArguments object.
        The expected order of values in the tuple is:
        conn_id, rec_id, ds_id, pt_id, random_seed, system_prompt, attack_module_id, prompt_index, prompt, target,
        predicted_results, and duration. It constructs a new PromptArguments instance using these values.
        The primary purpose of this method is to recreate PromptArguments instances from their serialized form, such as
        data retrieved from a database or received over a network.

        Args:
            cache_record (tuple): A tuple with ordered values that map to the properties of a PromptArguments instance.

        Returns:
            PromptArguments: An instance of PromptArguments initialized with the data from the tuple.
        """
        # The target and predicted_results fields may be stored as strings in the cache_record.
        # ast.literal_eval is used to attempt to convert these strings back into their original data types.
        # If the conversion fails (i.e., the fields are not string representations of Python literals),
        # the original string values are used.
        try:
            target = ast.literal_eval(cache_record[9])
        except Exception:
            target = cache_record[9]

        try:
            predicted_results_dict = json.loads(cache_record[10])
            predicted_results = ConnectorResponse(**predicted_results_dict)
        except Exception:
            predicted_results = cache_record[10]

        return cls(
            conn_id=cache_record[1],
            rec_id=cache_record[2],
            ds_id=cache_record[3],
            pt_id=cache_record[4],
            attack_module_id=cache_record[6],
            connector_prompt=ConnectorPromptArguments(
                prompt_index=cache_record[7],
                prompt=cache_record[8],
                target=target,
                predicted_results=predicted_results,
                duration=float(cache_record[11]),
            ),
            random_seed=cache_record[12],
            system_prompt=cache_record[13],
        )

# Ensure the model is rebuilt with all properties
PromptArguments.model_rebuild()