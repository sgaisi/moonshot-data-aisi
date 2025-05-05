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
from pydantic import BaseModel, Field

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
                tool_output_str = json.dumps(tool_output, indent=2)
            elif not isinstance(tool_output, str):
                tool_output_str = str(tool_output)
            else:
                tool_output_str = tool_output
            # Determine success/failure status
            is_success = not (isinstance(tool_output_str, str) and
                             (tool_output_str.startswith("Error:") or "error" in tool_output_str.lower()))

            # Add all information to the cleaned step
            cleaned_steps.append({
                "sequence_number": sequence_number,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": tool_output_str,
                "planning_reasoning": planning_reasoning,
                "is_success": is_success,
                "error_message": tool_output_str if not is_success else ""
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
                verbose=False,
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
        final_answer = "Error: Agent workflow failed to produce a final answer." 
        log_entry = {}
        if self.agent_executor is None:
            logger.error("Cannot process query: SingleAgentWorkflow was not initialized properly.")
            final_answer = "Error: Agent workflow could not be initialized."
            execution_time = time.time() - start_time
            log_entry = {
                "query": query,
                "execution_time": execution_time,
                "error": final_answer,
                "agents": {},
                "final_result": final_answer
                }
            return {"output": final_answer, "log": log_entry}

        try:
            logger.debug(f"Invoking agent executor with query: {query[:100]}...")
            result = await self.agent_executor.ainvoke({"input": query})
            full_output_raw = result.get("output", "")
            if isinstance(full_output_raw, str): full_output = full_output_raw.strip()
            elif isinstance(full_output_raw, list): full_output = " ".join(map(str, full_output_raw)).strip()
            elif full_output_raw is not None:
                full_output = str(full_output_raw).strip()
            else: full_output = ""
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
                    json_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```|(\{[\s\S]*\})', output_text, re.DOTALL)
                    if json_match:
                        potential_json = json_match.group(1) or json_match.group(2)
                        if potential_json: return json.loads(potential_json)
                except (json.JSONDecodeError, AttributeError): pass
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
                    "tool_use": None,
                    "parameters": None,
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
                        "tool_use": None, 
                        "parameters": None, 
                        "final_response": full_output
                        }
            final_answer = parsed_output.get("final_response", full_output) if isinstance(parsed_output, dict) else full_output
            if not isinstance(final_answer, str): final_answer = str(final_answer)

        except Exception as e:
            logger.error(f"Top-level processing error: {e}", exc_info=True)
            full_output = f"Error during execution: {e}"
            final_answer = f"Error processing request: {str(e)}"
            intermediate_steps = []
            parsed_output = {"error": final_answer, "planning": "", "tool_use": None, "parameters": None, "final_response": final_answer}


        executed_steps = []
        if intermediate_steps:
             executed_steps = process_intermediate_steps(intermediate_steps)
        else:
             executed_steps = [{ 
                                "sequence_number": 1, 
                                "tool_name": "N/A", "tool_input": {}, "tool_output": "No tool actions executed or recorded.", 
                                "planning_reasoning": "", 
                                "is_success": True, 
                                "error_message": "" }]
             
        execution_time = time.time() - start_time
        planning_output = parsed_output.get("planning", "") if isinstance(parsed_output, dict) else ""
        intended_tool_use_value = parsed_output.get("tool_use") if isinstance(parsed_output, dict) else None
        intended_parameters_value = parsed_output.get("parameters") if isinstance(parsed_output, dict) else None

        log_entry = {
            "query": query,
            "execution_time": execution_time,
            "agents": {
                "planner": {"role": "Task Planner", "plan": planning_output},
                "executed_tool_steps": {
                    "role": "Executed Tool Steps",
                    "executions": executed_steps
                },
                "response_generator": {"role": "Response Generator", "response": final_answer}
            },
            "final_result": final_answer
        }
        if "Error" in final_answer or parsed_output.get("error"):
             log_entry["error"] = final_answer if "Error" in final_answer else parsed_output.get("error")

        logger.debug(f"Agent processed query in {execution_time:.4f}s. Final Answer: {final_answer}...")

        return {
            "output": final_answer,
            "log": log_entry
        }


class Agentic:
    sql_create_runner_cache_record = """
        INSERT INTO runner_cache_table(
            connection_id, recipe_id, dataset_id, prompt_template_id, attack_module_id,
            prompt_index, prompt, target, predicted_results, duration,
            random_seed, system_prompt, dataset_tools
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    sql_read_runner_cache_record = """
        SELECT
            id, connection_id, recipe_id, dataset_id, prompt_template_id, attack_module_id,
            prompt_index, prompt, target, predicted_results, duration,
            random_seed, system_prompt, dataset_tools -- Added dataset_tools column
        FROM runner_cache_table
        WHERE connection_id=? AND recipe_id=? AND dataset_id=? AND prompt_template_id=? AND prompt=?
    """
    BATCH_SIZE = 10
    QUEUE_SIZE = 10

    def __init__(self):
        self._workflow_results_cache = {}
        self._agent_workflows = {}
        self._connector_llms: Dict[str, BaseChatModel] = {}
        self.all_tools: List[BaseTool] = []
        try:
            self.all_tools = get_all_tools()
            if not isinstance(self.all_tools, list) or not all(isinstance(t, BaseTool) for t in self.all_tools):
                 logger.warning("get_all_tools() did not return a valid list of BaseTool objects. Proceeding with an empty list.")
                 self.all_tools = []
            else:
                 self.all_tools_map = {tool.name: tool for tool in self.all_tools if hasattr(tool, 'name')}
                 logger.info(f"Initialized Agentic with {len(self.all_tools)} tools.")
        except Exception as e:
            logger.error(f"Failed to load tools during Agentic initialization: {e}", exc_info=True)
            self.all_tools = []
            self.all_tools_map = {}

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
                run_progress.notify_error(error_message)
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
            self.recipe_connectors = [Connector.create(ConnectorEndpoint.read(ep)) for ep in self.endpoints
            ]
            for connector in self.recipe_connectors:
                 connector.set_system_prompt(self.system_prompt)

            # ------------------------------------------------------------------------------
            # Part 1: Run the recipes and cookbooks
            # ------------------------------------------------------------------------------
            agentic_results = {}
            start_time = time.perf_counter()
            try:
                if self.cookbooks:
                    logger.info(f"[Agentic] Running cookbooks: {self.cookbooks}")
                    for idx, cookbook in enumerate(self.cookbooks):
                        self.run_progress.notify_progress(cookbook_index=idx, 
                            cookbook_name=cookbook,
                            cookbook_total=len(self.cookbooks)
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
                    for idx, recipe in enumerate(self.recipes):
                        self.run_progress.notify_progress(
                            recipe_index=idx, recipe_name=recipe, recipe_total=len(self.recipes)
                        )
                        agentic_results[recipe] = await self._run_recipe(recipe)
                    self.run_progress.notify_progress(recipe_index=len(self.recipes), raw_results=agentic_results)
                else:
                    # Unable to identify type
                    self.run_progress.notify_error("[Agentic] Failed to identify if agentic testing with cookbooks or recipes.")

            except Exception as e:
                 logger.error(f"[Agentic] Error during run: {e}", exc_info=True)
                 self.run_progress.notify_error(f"[Agentic] Failed run due to error: {str(e)}")
            finally:
                 logger.info(f"[Agentic] Run took {(time.perf_counter() - start_time):.4f}s")

        except Exception as e:
             logger.error(f"[Agentic] Failed setup before run: {e}", exc_info=True)
             if hasattr(self, 'run_progress'):
                 self.run_progress.notify_error(f"[Agentic] Failed to generate agentic due to error: {str(e)}")
             agentic_results = {}


        finally:
             logger.debug("[Agentic] Updating completion status...")
             final_status = RunStatus.COMPLETED
             if self.cancel_event.is_set():
                 final_status = RunStatus.CANCELLED
             elif self.run_progress.run_arguments.error_messages:
                 final_status = RunStatus.COMPLETED_WITH_ERRORS
             self.run_progress.notify_progress(
                 status=final_status
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
            logger.error(f"[Agentic] Failed to create ResultArguments: {e}", exc_info=True)
            self.run_progress.notify_error(f"[Agentic] Failed to prepare results due to error: {str(e)}"
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
        logger.debug(f"[Agentic] Load required instances...{cookbook_name}")
        try:
            self.cookbook_instance = Cookbook.load(cookbook_name)
        except Exception as e:
            self.run_progress.notify_error(f"Failed to load cookbook '{cookbook_name}': {e}")
            return {}
        # ------------------------------------------------------------------------------
        # Part 2: Run cookbook recipes
        # ------------------------------------------------------------------------------
        logger.debug("[Agentic] Running cookbook recipes...")
        recipes_results = {}
        if self.cookbook_instance:
            logger.info(f"Running recipes in cookbook '{cookbook_name}': {self.cookbook_instance.recipes}")
            for recipe_idx, recipe_name in enumerate(self.cookbook_instance.recipes):
                logger.debug(f"Running recipe '{recipe_name}' ({recipe_idx+1}/{len(self.cookbook_instance.recipes)})")
                self.run_progress.notify_progress(
                    recipe_index=recipe_idx, recipe_name=recipe_name, recipe_total=len(self.cookbook_instance.recipes)
                )
                try:
                    recipes_results[recipe_name] = await self._run_recipe(recipe_name)
                except Exception as e:
                    logger.error(f"Error running recipe '{recipe_name}' from cookbook '{cookbook_name}': {e}", exc_info=True)
                    self.run_progress.notify_error(f"Error in recipe '{recipe_name}': {e}")
                    recipes_results[recipe_name] = {"error": str(e)} # Include error marker

            self.run_progress.notify_progress(recipe_index=len(self.cookbook_instance.recipes)) # Mark recipe completion
        else:
             logger.error(f"Cookbook instance for '{cookbook_name}' was not loaded.")

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
        logger.debug(f"[Agentic] Loading recipe: {recipe_name}")
        start_time = time.perf_counter()
        self.recipe_instance = None
        self.recipe_metrics = []
        recipe_predictions = []
        recipe_results = {}
        try:
            # Load recipe
            self.recipe_instance = Recipe.load(recipe_name)
            self.recipe_metrics = [Metric.load(metric) for metric in self.recipe_instance.metrics]
            logger.debug(f"Loaded recipe '{recipe_name}' and {len(self.recipe_metrics)} metrics.")

        # ------------------------------------------------------------------------------
        # Part 2: Build and execute generator pipeline to get prompts and perform predictions
        # ------------------------------------------------------------------------------
            logger.debug(f"[Agentic] Starting generator pipeline for recipe '{recipe_name}'...")
            pipeline_task = self.event_loop.create_task(self._run_generator_pipeline(self.cancel_event))
            await pipeline_task
            recipe_predictions = pipeline_task.result() # Should be List[Optional[PromptArguments]]
            logger.debug(f"Generator pipeline finished for '{recipe_name}'. Predictions received: {len(recipe_predictions)}")
        # ------------------------------------------------------------------------------
        # Part 3: Sort the recipe predictions into groups for recipe
        # ------------------------------------------------------------------------------
        # Sort PromptArguments instances into groups based on the same conn_id, rec_id, ds_id, and pt_id
            # Filter out None values that resulted from prediction errors
            # Ensure recipe_predictions is actually a list before filtering
            valid_predictions = [p for p in recipe_predictions if isinstance(p, PromptArguments)]
            logger.debug(f"Valid predictions for grouping: {len(valid_predictions)}")

            if valid_predictions:
                valid_predictions.sort(key=attrgetter("conn_id", "rec_id", "ds_id", "pt_id"))
                grouped_recipe_preds = {
                    key: list(group) # Group is now List[PromptArguments]
                    for key, group in groupby(valid_predictions, key=attrgetter("conn_id", "rec_id", "ds_id", "pt_id"))
                }

                # Calculate metrics for each group
                logger.debug(f"Calculating metrics for {len(grouped_recipe_preds)} groups...")
                for group_key, group_list in grouped_recipe_preds.items():
                    conn_id, rec_id, ds_id, pt_id = group_key
                    logger.debug(f"Processing group: conn={conn_id}, rec={rec_id}, ds={ds_id}, pt={pt_id}")

                    # Prepare lists for metric calculation
                    prompts = [p.connector_prompt.prompt for p in group_list]
                    predicted_results_objs = [p.connector_prompt.predicted_results for p in group_list]
                    targets = [p.connector_prompt.target for p in group_list]
                    durations = [p.connector_prompt.duration for p in group_list]

        # ------------------------------------------------------------------------------
        # Part 4: Generate the metrics results
        # ------------------------------------------------------------------------------
                    metrics_result = []
                    for metric in self.recipe_metrics:
                        try:
                            metric_output = await metric.get_results(prompts, predicted_results_objs, targets)
                            metrics_result.append(metric_output)
                        except Exception as metric_err:
                            logger.error(f"Error calculating metric '{metric.id}' for group {group_key}: {metric_err}", exc_info=True)
                            metrics_result.append({"error": f"Metric calculation failed: {metric_err}"})


                    group_data = []
                    for i, p_arg in enumerate(group_list):
                         pred_res_dict = p_arg.connector_prompt.predicted_results.to_dict() if p_arg.connector_prompt.predicted_results else {"error": "No prediction"}
                         group_data.append({
                             "prompt": prompts[i],
                             "predicted_result": pred_res_dict,
                             "target": targets[i],
                             "duration": durations[i],
                             "dataset_tools_requested": p_arg.dataset_tools
                         })

                    recipe_results[group_key] = {
                        "data": group_data,
                        "results": metrics_result,
                    }
            else:
                 logger.warning(f"No valid predictions to process for recipe '{recipe_name}'.")


        except Exception as e:
            logger.error(f"[Agentic] Error running recipe '{recipe_name}': {e}", exc_info=True)
            self.run_progress.notify_error(f"Failed recipe '{recipe_name}': {str(e)}")
            recipe_results = {"error": f"Recipe execution failed: {str(e)}"} # Mark recipe result as error

        finally:
            logger.info(f"[Agentic] Finished recipe '{recipe_name}'. Took {(time.perf_counter() - start_time):.4f}s")
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
            gen_prompt = self._generate_prompts()
            # Create an asynchronous queue
            queue = asyncio.Queue(
                maxsize=Agentic.QUEUE_SIZE
            )  # Adjust the maxsize to control concurrency
            async def producer():
                try:
                    batch = []
                    prompt_count = 0
                    async for prompt_arg in gen_prompt: # Directly get PromptArguments
                        if cancel_event.is_set(): break
                        batch.append(prompt_arg)
                        prompt_count += 1
                        if len(batch) >= Agentic.BATCH_SIZE:
                            # Put the entire batch into the queue
                            await queue.put(batch)
                            batch = []  # Reset the batch

                    # If there are prompts left in the partial batch, put them in the queue
                    if batch:
                        await queue.put(batch)
                        logger.debug(f"Producer put final batch of {len(batch)} prompts into queue.")
                    logger.info(f"Producer finished generating {prompt_count} prompts.")
                finally:
                    # Signal the consumers that production is done or cancelled
                    await queue.put(None)

            # Consumer coroutine to process batches of prompts from the queue
            async def consumer():
                output = []
                processed_count = 0
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
                    connector_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                    # Process results from each connector for the batch
                    for result_set in connector_results:
                        if isinstance(result_set, Exception):
                            logger.error(f"[Agentic] Error in consumer gather: {result_set}", exc_info=result_set)
                            self.run_progress.notify_error(f"Prediction generation error: {result_set}")
                        elif isinstance(result_set, list):
                            output.extend(result_set) # Add results from this connector
                            processed_count += len(result_set)
                        else:
                             logger.warning(f"Unexpected result type from _generate_predictions: {type(result_set)}")

                    queue.task_done()
                    logger.debug(f"Consumer processed batch. Total processed so far: {processed_count}")
                logger.info(f"Consumer finished. Total results collected: {len(output)}")
                return output

            # Start the producer and consumer coroutines
            producer_task = asyncio.create_task(producer())
            consumer_task = asyncio.create_task(consumer())

            # Wait for the producer to finish generating prompts
            await producer_task
            await queue.join()
            await consumer_task

            final_results = consumer_task.result() if not consumer_task.cancelled() else []

            # Filter out None results again just in case
            return [res for res in final_results if res is not None]

        except Exception as e:
            logger.error(f"[Agentic] Error in generator pipeline: {e}", exc_info=True)
            self.run_progress.notify_error(f"Generator pipeline error: {str(e)}")
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
        logger.debug("[Agentic] Starting prompt generation...")
        templates = {}
        pt_id = "no-template" # Default if no templates used

        if self.recipe_instance.prompt_templates:
            logger.debug(f"Loading prompt templates: {self.recipe_instance.prompt_templates}")
            for pt_id_load in self.recipe_instance.prompt_templates:
                try:
                    # Assuming Storage.read_object_with_iterator yields template content correctly
                    pt_info_gen = Storage.read_object_with_iterator(
                        EnvVariables.PROMPT_TEMPLATES.name, pt_id_load, "json", iterator_keys=["template"]
                    )
                    pt_info = next(pt_info_gen["template"]) # Get template string
                    templates[pt_id_load] = Template(pt_info)
                except Exception as e:
                    logger.error(f"Failed to load prompt template '{pt_id_load}': {e}", exc_info=True)
                    self.run_progress.notify_error(f"Failed loading template '{pt_id_load}': {e}")
        else:
            logger.debug("No prompt templates specified in recipe.")


        for ds_id in self.recipe_instance.datasets:
            logger.debug(f"Processing dataset: {ds_id}")
            prompt_count_in_ds = 0
            # Use the modified _get_dataset_prompts which yields (index, data_dict)
            async for prompt_index, data_dict in self._get_dataset_prompts(ds_id):
                prompt_count_in_ds += 1
                # Extract necessary info from data_dict
                original_prompt_text = data_dict.get('input', '')
                target_info = data_dict.get('target', {}) # Target should be a dict now
                dataset_tool_names = data_dict.get('tools', []) # Get the list of tool names

                # --- Apply Attack Modules (Placeholder) ---
                # Assuming attack modules modify the prompt text.
                # If attacks modify tools, that logic needs integration here.
                # For now, assume only prompt text is modified.
                modified_prompts = [("no-attack", original_prompt_text)] # Example: (attack_id, modified_text)
                # -----------------------------------------

                if templates:
                    for pt_id_render, jinja2_template in templates.items():
                        for attack_id, mod_prompt_text in modified_prompts:
                            try:
                                rendered_prompt = jinja2_template.render({"prompt": mod_prompt_text})
                                # Yield PromptArguments with dataset_tool_names
                                yield await self._yield_prompt_arguments(
                                    pt_id=pt_id_render,
                                    ds_id=ds_id,
                                    attack_module_id=attack_id,
                                    prompt_index=prompt_index,
                                    prompt_text=rendered_prompt,
                                    target=target_info,
                                    dataset_tools=dataset_tool_names # Pass the list of names
                                )
                            except Exception as e:
                                logger.error(f"Error rendering template '{pt_id_render}' for prompt {prompt_index}: {e}", exc_info=True)
                                self.run_progress.notify_error(f"Template render error (prompt {prompt_index}): {e}")
                else: # No templates
                    for attack_id, mod_prompt_text in modified_prompts:
                         # Yield PromptArguments with dataset_tool_names
                         yield await self._yield_prompt_arguments(
                             pt_id=pt_id, # Use default "no-template"
                             ds_id=ds_id,
                             attack_module_id=attack_id,
                             prompt_index=prompt_index,
                             prompt_text=mod_prompt_text, # Use original/modified text
                             target=target_info,
                             dataset_tools=dataset_tool_names # Pass the list of names
                         )
            logger.debug(f"Finished processing {prompt_count_in_ds} prompts from dataset {ds_id}.")

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
        logger.debug(f"Getting prompts for dataset: {ds_id}")
        try:
            ds_args = Dataset.read(ds_id)
        except Exception as e:
             logger.error(f"Failed to read dataset '{ds_id}': {e}", exc_info=True)
             self.run_progress.notify_error(f"Failed reading dataset '{ds_id}': {e}")
             return # Stop generation for this dataset

        if not hasattr(ds_args, 'num_of_dataset_prompts') or ds_args.num_of_dataset_prompts == 0:
            logger.warning(f"Dataset {ds_id} is empty or num_of_dataset_prompts attribute missing/zero.")
            return
        prompt_indices = []
        try:
            num_to_select = max(
                1,
                int(
                    (self.prompt_selection_percentage / 100)
                    * ds_args.num_of_dataset_prompts
                ),
            )
            if num_to_select >= ds_args.num_of_dataset_prompts:
                prompt_indices = range(ds_args.num_of_dataset_prompts)
            else:
                random.seed(self.random_seed)
                prompt_indices = random.sample(range(ds_args.num_of_dataset_prompts), num_to_select)
            logger.debug(f"Selected {len(prompt_indices)} indices for dataset {ds_id}.")

        except Exception as e:
            logger.error(f"Error calculating prompt indices for {ds_id}: {e}", exc_info=True)
            # Fallback: use first N prompts or all if less than N
            fallback_count = min(10, ds_args.num_of_dataset_prompts)
            prompt_indices = range(fallback_count)
            logger.warning(f"Using fallback indices (first {fallback_count}) for dataset {ds_id}.")

        # Check if examples attribute exists
        if not hasattr(ds_args, 'examples') or not ds_args.examples:
            logger.error(f"Dataset {ds_id} has no 'examples' attribute or it's empty.")
            return

        prompts_gen_index = 0
        yielded_count = 0
        try:
            
            is_list = isinstance(ds_args.examples, list)

            for current_index, prompts_data in enumerate(ds_args.examples):
                if current_index in prompt_indices:
                    try:
                        if isinstance(prompts_data, dict):
                            input_text = prompts_data.get('input', '')
                            # Ensure 'tools' is a list of strings
                            dataset_tools_raw = prompts_data.get('tools', [])
                            if isinstance(dataset_tools_raw, list) and all(isinstance(t, str) for t in dataset_tools_raw):
                                dataset_tools = dataset_tools_raw
                            else:
                                logger.warning(f"Invalid 'tools' format in dataset {ds_id} (index {current_index}). Expected List[str]. Got: {type(dataset_tools_raw)}. Using empty list.")
                                dataset_tools = []

                            # Ensure target is a dict
                            target_data = prompts_data.get('target', {})
                            if not isinstance(target_data, dict):
                                target_data = {'description': str(target_data)}

                            # Other fields
                            task_name = prompts_data.get('task_name', '')
                            task_category = prompts_data.get('task_category', '')
                            initial_content = prompts_data.get('initial_content', {})
                            # Create converted data structure
                            converted_data = {
                                'input': input_text,
                                'target': target_data,
                                'tools': dataset_tools,
                                'task_name': task_name,
                                'task_category': task_category,
                                'initial_content': initial_content
                            }
                            yield current_index, converted_data
                            yielded_count += 1
                        else:
                            logger.warning(f"Unexpected data type at index {current_index} in dataset {ds_id}: {type(prompts_data)}. Skipping.")

                    except Exception as item_err:
                        logger.error(f"Error processing item at index {current_index} in dataset {ds_id}: {item_err}", exc_info=True)
                        # Yield error structure
                        yield current_index, {'input': f"Error processing: {item_err}", 'target': {}, 'tools': [], 'error': str(item_err)}
                        yielded_count += 1

                if is_list and yielded_count >= len(prompt_indices):
                     break

                prompts_gen_index += 1

        except Exception as iter_err:
             logger.error(f"Error iterating through examples for dataset {ds_id}: {iter_err}", exc_info=True)
             self.run_progress.notify_error(f"Dataset iteration error ({ds_id}): {iter_err}")

        logger.debug(f"Finished getting prompts for dataset {ds_id}. Yielded: {yielded_count}")

    async def _yield_prompt_arguments(
        self,
        pt_id: str,
        ds_id: str,
        attack_module_id: str,
        prompt_index: int,
        prompt_text: str,
        target: Any,
        dataset_tools: List[str],
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
            dataset_tools=dataset_tools,
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
    ) -> list[PromptArguments | None]:
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

        processed_results = []
        for i, result in enumerate(results):
            original_prompt_index = prompt_batch[i].connector_prompt.prompt_index
            if isinstance(result, Exception):
                logger.error(f"Error processing prompt index {original_prompt_index} with connector {connector.id}: {result}", exc_info=result)
                self.run_progress.notify_error(f"Prediction failed (prompt {original_prompt_index}, conn {connector.id}): {result}")
                processed_results.append(None) # Append None for failures
            elif result is None: # Handle explicit None returns (e.g., cancellation)
                 logger.warning(f"Processing returned None for prompt index {original_prompt_index} (likely cancelled).")
                 processed_results.append(None)
            elif isinstance(result, PromptArguments):
                processed_results.append(result)
            else:
                 logger.error(f"Unexpected result type {type(result)} for prompt index {original_prompt_index}. Appending None.")
                 processed_results.append(None) # Handle unexpected types

        return processed_results # Return list potentially containing None

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
            logger.warning(f"[Agentic] Cancellation requested for prompt index {prompt_info.connector_prompt.prompt_index}.")
            return None # Return None for cancelled operations
        # Create a new prompt info object for modification and add connector ID
        new_prompt_info = copy.deepcopy(prompt_info)
        # Assign connector ID if not present or mismatched
        if not new_prompt_info.conn_id: new_prompt_info.conn_id = connector.id
        elif new_prompt_info.conn_id != connector.id: new_prompt_info.conn_id = connector.id

        # --- Check Cache ---
        cache_record = None
        if hasattr(self, 'database_instance') and self.database_instance:
            try:
                # Ensure all parameters for the query are present in new_prompt_info
                query_params = (
                    new_prompt_info.conn_id,
                    new_prompt_info.rec_id,
                    new_prompt_info.ds_id,
                    new_prompt_info.pt_id,
                    new_prompt_info.connector_prompt.prompt,
                )
                cache_record = Storage.read_database_record(
                    self.database_instance,
                    query_params,
                    Agentic.sql_read_runner_cache_record,
                )
                if cache_record:
                    logger.info(f"Cache hit for prompt index {new_prompt_info.connector_prompt.prompt_index} (conn: {new_prompt_info.conn_id})")
            except Exception as e:
                # Log error but continue, as if cache missed
                logger.error(f"[Agentic] Error reading cache {new_prompt_info.connector_prompt.prompt_index}: {e}", exc_info=True)
                self.run_progress.notify_error(f"Cache read error: {e}")
                cache_record = None

        # --- Process if Cache Missed ---
        if cache_record is None:
            logger.info(f"Cache miss for prompt index {new_prompt_info.connector_prompt.prompt_index}. Processing...")
            try:
                query = new_prompt_info.connector_prompt.prompt

                # --- Tool Selection Logic ---
                specified_tool_names = new_prompt_info.dataset_tools # Get List[str]
                tools_for_this_prompt: Optional[List[BaseTool]] = None # Default to None (-> use all tools)

                if specified_tool_names: # Only filter if names were provided
                    logger.debug(f"Dataset specified tools for prompt {new_prompt_info.connector_prompt.prompt_index}: {specified_tool_names}")
                    current_matched_tools = [
                        self.all_tools_map[name]
                        for name in specified_tool_names
                        if name in self.all_tools_map
                    ]

                    if len(current_matched_tools) != len(specified_tool_names):
                        found_names = {t.name for t in current_matched_tools}
                        missing_names = set(specified_tool_names) - found_names
                        logger.warning(f"Prompt {new_prompt_info.connector_prompt.prompt_index}: Could not find all specified tools. Missing: {missing_names}.")

                    if current_matched_tools:
                        tools_for_this_prompt = current_matched_tools
                        logger.info(f"Using SPECIFIC tools for prompt {new_prompt_info.connector_prompt.prompt_index}: {[t.name for t in tools_for_this_prompt]}")
                    else:
                        logger.warning(f"Prompt {new_prompt_info.connector_prompt.prompt_index}: None of the specified tools ({specified_tool_names}) were found. Using DEFAULT tools.")
                else:
                    logger.info(f"Using DEFAULT (all) tools for prompt {new_prompt_info.connector_prompt.prompt_index}.")


                # --- Get Agent Workflow ---
                # Pass the filtered list *only if* specific tools were identified.
                agent_workflow = await self._get_or_create_agent_workflow(
                    connector,
                    tools_for_this_prompt
                )

                # --- Execute Workflow ---
                if agent_workflow:
                    logger.debug(f"Invoking agent workflow for prompt index {new_prompt_info.connector_prompt.prompt_index}")
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
                    error_msg = f"Agent workflow creation/retrieval failed for connector {connector.id}."
                    logger.error(f"[Agentic] {error_msg} for prompt {new_prompt_info.connector_prompt.prompt_index}")
                    new_prompt_info.connector_prompt.predicted_results = ConnectorResponse(response=error_msg, context=[{"error": error_msg}])
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
                        self.run_progress.notify_error(f"Failed to cache results: {e}")
                else:
                     logger.warning("[Agentic] Database instance not available, cannot cache results.")


            except Exception as e:
                logger.error(f"[Agentic] FATAL error processing prompt index {new_prompt_info.connector_prompt.prompt_index}: {e}", exc_info=True)
                self.run_progress.notify_error(f"Failed processing prompt {new_prompt_info.connector_prompt.prompt_index}: {e}")
                new_prompt_info.connector_prompt.predicted_results = ConnectorResponse(response=f"Fatal Error: {e}", context=[{"error": str(e)}])
                new_prompt_info.connector_prompt.duration = 0.0
                # Return None to indicate failure for this prompt
                return new_prompt_info


        # --- Process if Cache Hit ---
        else: # cache_record is not None
            logger.debug(f"Loading result from cache for prompt index {new_prompt_info.connector_prompt.prompt_index}")
            try:
                # Reconstitute the PromptArguments object from the database tuple
                # Ensure PromptArguments.from_tuple handles potential errors robustly
                new_prompt_info = PromptArguments.from_tuple(cache_record)
                # Verify essential data loaded correctly
                if not new_prompt_info.connector_prompt.predicted_results:
                     logger.warning(f"Cached data for prompt_index {new_prompt_info.connector_prompt.prompt_index} missing predicted_results.")
                     # Optionally handle this case, maybe treat as cache miss?

            except Exception as e:
                logger.error(f"[Agentic] Failed to load prompt info from cache record: {e}. Cache Record: {cache_record}", exc_info=True)
                self.run_progress.notify_error(f"Failed loading from cache: {e}")
                # Return None as we couldn't process or load from cache
                return None


        # Return the updated PromptArguments object (either newly processed or from cache)
        return new_prompt_info

    async def _get_or_create_agent_workflow(
        self,
        connector: Connector,
        specific_tools: Optional[List[BaseTool]] = None # Accepts Optional[List[BaseTool]]
    ) -> Optional[SingleAgentWorkflow]:
        """
        Gets or creates an initialized LLM instance for a given connector.
        Supports multiple LLM providers including OpenAI, Azure OpenAI, Anthropic,
        Together AI, AWS Bedrock, etc.
        """
        # Determine the tool list and cache key
        if specific_tools is not None: # specific_tools could be [] or a list
            # Validate it's a list of BaseTool objects
            if isinstance(specific_tools, list) and all(isinstance(t, BaseTool) for t in specific_tools):
                tools_list = specific_tools
                # Create a stable cache key based on sorted tool names
                tool_names = sorted([getattr(t, 'name', f'unnamed_{i}') for i, t in enumerate(tools_list)])
                tools_key = "-".join(tool_names) if tool_names else "specific_empty_list"
                logger.info(f"Requesting workflow with SPECIFIC tools ({len(tools_list)}): {tool_names}")
            else:
                # Invalid specific_tools provided, log error and fallback to default
                logger.error(f"Invalid specific_tools list provided: {specific_tools}. Falling back to default tools.")
                tools_list = self.all_tools # Use pre-loaded default tools
                tools_key = "all_default_tools"
        else:
            # No specific tools requested, use the default set
            tools_list = self.all_tools
            tools_key = "all_default_tools" # Cache key for the default set
            logger.info(f"Requesting workflow with DEFAULT tools ({len(tools_list)}).")

        # Final cache key combines connector ID and toolset identifier
        cache_key = f"{connector.id}:{tools_key}"

        # Check cache
        if cache_key in self._agent_workflows:
            logger.debug(f"Reusing existing agent workflow for cache key: {cache_key}")
            return self._agent_workflows[cache_key]

        # --- Create New Workflow ---
        logger.info(f"Creating new agent workflow for cache key: {cache_key}")
        try:
            # Get LLM for the connector
            llm = await self._get_llm_for_connector(connector)
            if not llm:
                logger.error(f"Failed to get LLM for connector {connector.id}. Cannot create workflow.")
                return None # Cannot proceed without LLM
            agent_workflow = SingleAgentWorkflow(llm=llm, tools=tools_list)

            # Check if workflow initialization failed internally
            if agent_workflow.agent_executor is None:
                 logger.error(f"SingleAgentWorkflow initialization failed internally for key {cache_key}.")
                 # Don't cache a failed workflow instance
                 return None

            # Cache the successfully created workflow
            self._agent_workflows[cache_key] = agent_workflow
            logger.info(f"Successfully created and cached agent workflow for key: {cache_key}")
            return agent_workflow

        except Exception as e:
            logger.error(f"[Agentic Workflow Creation] Failed for key {cache_key}: {e}", exc_info=True)
            self.run_progress.notify_error(f"Failed agent workflow creation ({connector.id}, tools '{tools_key}'): {e}")
            return None


    # --- _get_llm_for_connector method ---
    async def _get_llm_for_connector(self, connector: Connector) -> Optional[BaseChatModel]:
        """Gets or creates an LLM instance for a connector, caching the result."""
        # ... (LLM creation/caching logic - remains the same as provided previously) ...
        if connector.id in self._connector_llms:
            logger.debug(f"Reusing cached LLM for connector ID: {connector.id}")
            return self._connector_llms[connector.id]

        logger.info(f"Creating new LLM instance for connector ID: {connector.id}")
        llm = None
        temperature = self.temperature
        connector_type_str = str(type(connector)).lower()
        try:
            # Extract common parameters that might be used across different providers
            model_name = getattr(connector, 'model', getattr(connector, 'model_id', None)) # More robust model name fetching
            api_key = getattr(connector, 'token', None)
            base_url = getattr(connector, 'endpoint', None)
            region = getattr(connector, 'region', None) # For Bedrock etc.
            api_version = getattr(connector, 'api_version', None) # For Azure
            deployment_name = getattr(connector, 'deployment_name', model_name) # For Azure

            logger.debug(f"Connector Info - Type: {connector_type_str}, ID: {connector.id}, Model: {model_name}, Temp: {temperature}, Endpoint: {base_url}, Region: {region}")
            if "azure" in connector_type_str or "azureopenai" in connector_type_str:
                 if not deployment_name or not base_url or not api_key or not api_version:
                      raise ValueError("Azure OpenAI connector missing required fields (deployment, endpoint, key, api_version)")
                 llm = AzureChatOpenAI(
                     deployment_name=deployment_name, 
                     model_name=model_name,
                     temperature=temperature,
                     openai_api_key=api_key,
                     azure_endpoint=base_url,
                     api_version=api_version,
                 )
                 logger.info(f"Initialized AzureChatOpenAI LLM for {connector.id}")
            elif "openai" in connector_type_str: # Must come after Azure check
                 if not model_name: raise ValueError("OpenAI connector missing 'model' name")
                 llm = ChatOpenAI(
                     model=model_name, 
                     temperature=temperature,
                     openai_api_key=api_key, 
                     openai_api_base=base_url, 
                 )
                 logger.info(f"Initialized ChatOpenAI LLM for {connector.id}")
            elif "anthropic" in connector_type_str or ("claude" in model_name.lower() if model_name else False):
                 if not model_name: raise ValueError("Anthropic connector missing 'model' name")
                 llm = ChatAnthropic(
                     model=model_name, temperature=temperature,
                     anthropic_api_key=api_key, api_url=base_url if base_url else None,
                 )
                 logger.info(f"Initialized ChatAnthropic LLM for {connector.id}")
            elif "bedrock" in connector_type_str:
                 if not model_name: raise ValueError("Bedrock connector missing 'model_id'")
                 # Credentials should be handled via environment or AWS config
                 llm = ChatBedrockConverse(
                     model_id=model_name,
                     region_name=region, 
                     temperature=temperature,
                 )
                 logger.info(f"Initialized Bedrock ChatBedrockConverse LLM for {connector.id}")
            elif "together" in connector_type_str:
                 if not model_name: raise ValueError("Together AI connector missing 'model' name")
                 llm = ChatTogether(
                     model=model_name,
                     temperature=temperature,
                     together_api_key=api_key,
                 )
                 logger.info(f"Initialized Together LLM for {connector.id}")
            elif "anthropic" in connector_type_str:
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
            self.run_progress.notify_error(f"Failed ({connector.id}): {e}")
            return None


class PromptArguments(BaseModel):
    conn_id: str = ""
    rec_id: str
    ds_id: str
    pt_id: str
    dataset_tools: List[str] = Field(default_factory=list)
    random_seed: int
    system_prompt: str
    attack_module_id: str
    connector_prompt: ConnectorPromptArguments

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
        # Serialize complex fields
        target_str = json.dumps(self.connector_prompt.target) if isinstance(self.connector_prompt.target, dict) else str(self.connector_prompt.target)
        pred_res_str = json.dumps(self.connector_prompt.predicted_results.to_dict()) if self.connector_prompt.predicted_results else "{}"
        dataset_tools_str = json.dumps(self.dataset_tools) # Serialize list of names

        # Order must match sql_create_runner_cache_record VALUES clause
        return (
            self.conn_id,
            self.rec_id,
            self.ds_id,
            self.pt_id,
            self.attack_module_id,
            self.connector_prompt.prompt_index,
            self.connector_prompt.prompt,
            target_str,
            pred_res_str,
            str(self.connector_prompt.duration),
            self.random_seed,
            self.system_prompt,
            dataset_tools_str
        )

    @classmethod
    def from_tuple(cls, cache_record: tuple) -> "PromptArguments":
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
            conn_id = cache_record[1]
            rec_id = cache_record[2]
            ds_id = cache_record[3]
            pt_id = cache_record[4]
            attack_module_id = cache_record[5]
            prompt_index = cache_record[6]
            prompt_text = cache_record[7]
            target_str = cache_record[8]
            pred_res_str = cache_record[9]
            duration_str = cache_record[10]
            random_seed = cache_record[11]
            system_prompt = cache_record[12]
            dataset_tools_str = cache_record[13]
            # Deserialize complex fields
            try: target = json.loads(target_str)
            except (json.JSONDecodeError, TypeError): target = target_str # Keep as string if not valid JSON

            predicted_results = None
            try:
                pred_res_dict = json.loads(pred_res_str)
                if pred_res_dict: # Ensure dict is not empty
                     predicted_results = ConnectorResponse(**pred_res_dict)
            except (json.JSONDecodeError, TypeError):
                 logger.warning(f"Could not parse predicted_results from cache: {pred_res_str}")
                 predicted_results = ConnectorResponse(response="Error: Could not load from cache", context=[]) # Default on error

            try: duration = float(duration_str)
            except (ValueError, TypeError): duration = 0.0

            try: dataset_tools = json.loads(dataset_tools_str)
            except (json.JSONDecodeError, TypeError):
                 logger.warning(f"Could not parse dataset_tools from cache: {dataset_tools_str}")
                 dataset_tools = [] # Default to empty list on error

            # Validate dataset_tools is a list of strings
            if not isinstance(dataset_tools, list) or not all(isinstance(t, str) for t in dataset_tools):
                logger.warning(f"Invalid dataset_tools format loaded from cache. Expected List[str], got {type(dataset_tools)}. Resetting to empty list.")
                dataset_tools = []


            return cls(
                conn_id=conn_id,
                rec_id=rec_id,
                ds_id=ds_id,
                pt_id=pt_id,
                dataset_tools=dataset_tools,
                random_seed=random_seed,
                system_prompt=system_prompt,
                attack_module_id=attack_module_id,
                connector_prompt=ConnectorPromptArguments(
                    prompt_index=prompt_index,
                    prompt=prompt_text,
                    target=target,
                    predicted_results=predicted_results,
                    duration=duration,
                ),
            )
        except IndexError as e:
             logger.error(f"Cache record tuple has incorrect length or missing fields: {e}. Record: {cache_record}", exc_info=True)
             raise ValueError(f"Failed to load PromptArguments from cache due to missing fields: {e}") from e
        except Exception as e:
             logger.error(f"Unexpected error loading PromptArguments from cache: {e}. Record: {cache_record}", exc_info=True)
             raise ValueError(f"Failed to load PromptArguments from cache: {e}") from e


# Ensure the model is rebuilt with all properties
PromptArguments.model_rebuild()