from __future__ import annotations

import dataclasses
import json
import time
import os
import litellm
import tiktoken
import inspect
import hashlib
import re
import asyncio

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast, overload
from cai.util import get_ollama_api_base, fix_message_list, cli_print_agent_messages, create_agent_streaming_context, update_agent_streaming_content, finish_agent_streaming
from cai.util import start_idle_timer, stop_idle_timer, start_active_timer, stop_active_timer
from wasabi import color
from cai.sdk.agents.run_to_jsonl import get_session_recorder

from openai import NOT_GIVEN, AsyncOpenAI, AsyncStream, NotGiven
from openai.types import ChatModel
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionChunk,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionDeveloperMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam
from openai.types.chat.completion_create_params import ResponseFormat
from openai.types.completion_usage import CompletionUsage
from openai.types.responses import (
    EasyInputMessageParam,
    Response,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseFileSearchToolCallParam,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallParam,
    ResponseInputContentParam,
    ResponseInputImageParam,
    ResponseInputTextParam,
    ResponseOutputItem,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputMessageParam,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseRefusalDeltaEvent,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_input_param import FunctionCallOutput, ItemReference, Message
from openai.types.responses.response_usage import OutputTokensDetails
from cai.util import calculate_model_cost
# Create custom InputTokensDetails class since it's not available in current OpenAI version
from openai._models import BaseModel
class InputTokensDetails(BaseModel):
    prompt_tokens: int
    """The number of prompt tokens."""
    cached_tokens: int = 0
    """The number of cached tokens."""
    
# Custom ResponseUsage that makes prompt_tokens/input_tokens and completion_tokens/output_tokens compatible
class CustomResponseUsage(ResponseUsage):
    """
    Custom ResponseUsage class that provides compatibility between different field naming conventions.
    Works with both input_tokens/output_tokens and prompt_tokens/completion_tokens.
    """
    @property
    def prompt_tokens(self) -> int:
        """Alias for input_tokens to maintain compatibility"""
        return self.input_tokens
        
    @property
    def completion_tokens(self) -> int:
        """Alias for output_tokens to maintain compatibility"""
        return self.output_tokens

from .. import _debug
from ..agent_output import AgentOutputSchema
from ..exceptions import AgentsException, UserError
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseOutputItem, TResponseStreamEvent
from ..logger import logger
from ..tool import FunctionTool, Tool
from ..tracing import generation_span
from ..tracing.span_data import GenerationSpanData
from ..tracing.spans import Span
from ..usage import Usage
from ..version import __version__
from .fake_id import FAKE_RESPONSES_ID
from .interface import Model, ModelTracing

if TYPE_CHECKING:
    from ..model_settings import ModelSettings


# Suppress debug info from litellm
litellm.suppress_debug_info = True

if os.getenv('CAI_MODEL') == "o3-mini" or os.getenv('CAI_MODEL') == "gemini-1.5-pro": 
    litellm.drop_params = True

_USER_AGENT = f"Agents/Python {__version__}"
_HEADERS = {"User-Agent": _USER_AGENT}

message_history = []

# Function to add a message to history if it's not a duplicate
def add_to_message_history(msg):
    """Add a message to history if it's not a duplicate."""
    if not message_history:
        message_history.append(msg)
        return

    is_duplicate = False

    if msg.get("role") in ["system", "user"]:
        is_duplicate = any(
            existing.get("role") == msg.get("role") and 
            existing.get("content") == msg.get("content")
            for existing in message_history
        )

    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
        is_duplicate = any(
            existing.get("role") == "assistant" and 
            existing.get("tool_calls") and 
            existing["tool_calls"][0].get("id") == msg["tool_calls"][0].get("id")
            for existing in message_history
        )

    if not is_duplicate:
        message_history.append(msg)

@dataclass
class _StreamingState:
    started: bool = False
    text_content_index_and_output: tuple[int, ResponseOutputText] | None = None
    refusal_content_index_and_output: tuple[int, ResponseOutputRefusal] | None = None
    function_calls: dict[int, ResponseFunctionToolCall] = field(default_factory=dict)


# Add a new function for consistent token counting using tiktoken
def count_tokens_with_tiktoken(text_or_messages):
    """
    Count tokens consistently using tiktoken library.
    Works with both strings and message lists.
    Returns a tuple of (input_tokens, reasoning_tokens).
    """
    if not text_or_messages:
        return 0, 0
        
    try:
        # Try to use cl100k_base encoding (used by GPT-4 and GPT-3.5-turbo)
        encoding = tiktoken.get_encoding("cl100k_base")
    except:
        # Fall back to GPT-2 encoding if cl100k is not available
        try:
            encoding = tiktoken.get_encoding("gpt2")
        except:
            # If tiktoken fails, fall back to character estimate
            if isinstance(text_or_messages, str):
                return len(text_or_messages) // 4, 0
            elif isinstance(text_or_messages, list):
                total_len = 0
                for msg in text_or_messages:
                    if isinstance(msg, dict) and 'content' in msg:
                        if isinstance(msg['content'], str):
                            total_len += len(msg['content'])
                return total_len // 4, 0
            else:
                return 0, 0
    
    # Process different input types
    if isinstance(text_or_messages, str):
        token_count = len(encoding.encode(text_or_messages))
        return token_count, 0
    elif isinstance(text_or_messages, list):
        total_tokens = 0
        reasoning_tokens = 0
        
        # Add tokens for the messages format (ChatML format overhead)
        # Each message has a base overhead (usually ~4 tokens)
        total_tokens += len(text_or_messages) * 4
        
        for msg in text_or_messages:
            if isinstance(msg, dict):
                # Add tokens for role
                if 'role' in msg:
                    total_tokens += len(encoding.encode(msg['role']))
                
                # Count content tokens
                if 'content' in msg and msg['content']:
                    if isinstance(msg['content'], str):
                        content_tokens = len(encoding.encode(msg['content']))
                        total_tokens += content_tokens
                        
                        # Count tokens in assistant messages as reasoning tokens
                        if msg.get('role') == 'assistant':
                            reasoning_tokens += content_tokens
                    elif isinstance(msg['content'], list):
                        for content_part in msg['content']:
                            if isinstance(content_part, dict) and 'text' in content_part:
                                part_tokens = len(encoding.encode(content_part['text']))
                                total_tokens += part_tokens
                                if msg.get('role') == 'assistant':
                                    reasoning_tokens += part_tokens
        
        return total_tokens, reasoning_tokens
    else:
        return 0, 0


class OpenAIChatCompletionsModel(Model):
    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
    ) -> None:
        self.model = model
        self._client = openai_client
        # Check if we're using OLLAMA models
        self.is_ollama = os.getenv('OLLAMA') is not None and os.getenv('OLLAMA').lower() != 'false'
        self.empty_content_error_shown = False
        
        # Track interaction counter and token totals for cli display
        self.interaction_counter = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_reasoning_tokens = 0
        self.total_cost = 0.0
        self.agent_name = "Agent"  # Default name
        
        # Flags for CLI integration
        self.disable_rich_streaming = False    # Prevents creating a rich panel in the model
        self.suppress_final_output = False     # Prevents duplicate output at end of streaming
        
        # Initialize the session logger
        self.logger = get_session_recorder()
        
    def set_agent_name(self, name: str) -> None:
        """Set the agent name for CLI display purposes."""
        self.agent_name = name
        
    def _non_null_or_not_given(self, value: Any) -> Any:
        return value if value is not None else NOT_GIVEN

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
    ) -> ModelResponse:
        # Increment the interaction counter for CLI display
        self.interaction_counter += 1
        
        # Stop idle timer and start active timer to track LLM processing time
        stop_idle_timer()
        start_active_timer()
        
        with generation_span(
            model=str(self.model),
            model_config=dataclasses.asdict(model_settings)
            | {"base_url": str(self._client.base_url)},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            # Prepare the messages for consistent token counting
            converted_messages = _Converter.items_to_messages(input)
            if system_instructions:
                converted_messages.insert(
                    0,
                    {
                        "content": system_instructions,
                        "role": "system",
                    },
                )
            # --- Add to message_history: user, system, and assistant tool call messages ---
            # Add system prompt to message_history
            if system_instructions:
                sys_msg = {
                    "role": "system",
                    "content": system_instructions
                }
                add_to_message_history(sys_msg)
                
            # Add user prompt(s) to message_history
            if isinstance(input, str):
                user_msg = {
                    "role": "user",
                    "content": input
                }
                add_to_message_history(user_msg)
                # Log the user message
                self.logger.log_user_message(input)
            elif isinstance(input, list):
                for item in input:
                    # Try to extract user messages
                    if isinstance(item, dict):
                        if item.get("role") == "user":
                            user_msg = {
                                "role": "user",
                                "content": item.get("content", "")
                            }
                            add_to_message_history(user_msg)
                            # Log the user message
                            if item.get("content"):
                                self.logger.log_user_message(item.get("content"))
            # Get token count estimate before API call for consistent counting
            estimated_input_tokens, _ = count_tokens_with_tiktoken(converted_messages)
            
            response = await self._fetch_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span_generation,
                tracing,
                stream=False,
            )

            if _debug.DONT_LOG_MODEL_DATA:
                logger.debug("Received model response")
            else:
                logger.debug(
                    f"LLM resp:\n{json.dumps(response.choices[0].message.model_dump(), indent=2)}\n"
                )

            # Ensure we have reasonable token counts
            if response.usage:
                input_tokens = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens
                total_tokens = response.usage.total_tokens
                
                # Use estimated tokens if API returns zeroes or implausible values
                if input_tokens == 0 or input_tokens < (len(str(input)) // 10):  # Sanity check
                    input_tokens = estimated_input_tokens
                    total_tokens = input_tokens + output_tokens
                
                # # Debug information
                # print(f"\nDEBUG CONSISTENT TOKEN COUNTS - API tokens: input={input_tokens}, output={output_tokens}, total={total_tokens}")
                # print(f"Estimated tokens were: input={estimated_input_tokens}")
            else:
                # If no usage info, use our estimates
                input_tokens = estimated_input_tokens
                output_tokens = 0
                total_tokens = input_tokens
                # print(f"\nDEBUG CONSISTENT TOKEN COUNTS - No API tokens, using estimates: input={input_tokens}, output={output_tokens}")

            
            # Update token totals for CLI display
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            if (response.usage and 
                hasattr(response.usage, 'completion_tokens_details') and 
                response.usage.completion_tokens_details and 
                hasattr(response.usage.completion_tokens_details, 'reasoning_tokens')):
                self.total_reasoning_tokens += response.usage.completion_tokens_details.reasoning_tokens

            # Check if this message contains tool calls
            tool_output = None
            should_display_message = True

            if (hasattr(response.choices[0].message, 'tool_calls') and 
                response.choices[0].message.tool_calls):
                
                # For each tool call in the message, get corresponding output if available
                for tool_call in response.choices[0].message.tool_calls:
                    call_id = tool_call.id
                    
                    # If we're using direct tool output display with cli_print_tool_output,
                    # and we've already displayed this tool call output, we can skip displaying
                    # the assistant message to avoid duplication
                    if (hasattr(_Converter, 'tool_outputs') and call_id in _Converter.tool_outputs and
                        hasattr(_Converter, 'recent_tool_calls') and call_id in _Converter.recent_tool_calls):
                        # We've already displayed this tool and its output directly
                        should_display_message = False
                        break

            # Only display the agent message if we haven't already shown the tool output
            if should_display_message:
                # Print the agent message for CLI display
                cli_print_agent_messages(
                    agent_name=getattr(self, 'agent_name', 'Agent'),
                    message=response.choices[0].message,
                    counter=getattr(self, 'interaction_counter', 0),
                    model=str(self.model),
                    debug=False,
                    interaction_input_tokens=input_tokens,
                    interaction_output_tokens=output_tokens,
                    interaction_reasoning_tokens=(
                        response.usage.completion_tokens_details.reasoning_tokens 
                        if response.usage and hasattr(response.usage, 'completion_tokens_details') 
                        and response.usage.completion_tokens_details
                        and hasattr(response.usage.completion_tokens_details, 'reasoning_tokens')
                        else 0
                    ),
                    total_input_tokens=getattr(self, 'total_input_tokens', 0),
                    total_output_tokens=getattr(self, 'total_output_tokens', 0),
                    total_reasoning_tokens=getattr(self, 'total_reasoning_tokens', 0),
                    interaction_cost=None,
                    total_cost=None,
                    tool_output=None,  # Don't pass tool output here, we're using direct display
                )

            # --- Add assistant tool call to message_history if present ---
            # If the response contains tool_calls, add them to message_history as assistant messages
            assistant_msg = response.choices[0].message
            if hasattr(assistant_msg, "tool_calls") and assistant_msg.tool_calls:
                for tool_call in assistant_msg.tool_calls:
                    # Compose a message for the tool call
                    tool_call_msg = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call.id,
                                "type": tool_call.type,
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": tool_call.function.arguments
                                }
                            }
                        ]
                    }
                    
                    add_to_message_history(tool_call_msg)
                
                # Log the assistant tool call message
                tool_calls_list = []
                for tool_call in assistant_msg.tool_calls:
                    tool_calls_list.append({
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments
                        }
                    })
                self.logger.log_assistant_message(None, tool_calls_list)
            # If the assistant message is just text, add it as well
            elif hasattr(assistant_msg, "content") and assistant_msg.content:
                asst_msg = {
                    "role": "assistant",
                    "content": assistant_msg.content
                }
                add_to_message_history(asst_msg)
                # Log the assistant message
                self.logger.log_assistant_message(assistant_msg.content)
            
            # Log the complete response for the session
            self.logger.rec_training_data(
                {
                    "model": str(self.model),
                    "messages": converted_messages,
                    "stream": False,
                    "tools": [t.params_json_schema for t in tools] if tools else [],
                    "tool_choice": model_settings.tool_choice
                },
                response,
                self.total_cost
            )

            usage = (
                Usage(
                    requests=1,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                )
                if response.usage or input_tokens > 0
                else Usage()
            )
            if tracing.include_data():
                span_generation.span_data.output = [response.choices[0].message.model_dump()]
            span_generation.span_data.usage = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }

            items = _Converter.message_to_output_items(response.choices[0].message)
            
            # For non-streaming responses, make sure we also log token usage with compatible field names
            # This ensures both streaming and non-streaming use consistent naming
            if not hasattr(response, 'usage'):
                response.usage = {}
            if hasattr(response.usage, 'prompt_tokens') and not hasattr(response.usage, 'input_tokens'):
                response.usage.input_tokens = response.usage.prompt_tokens
            if hasattr(response.usage, 'completion_tokens') and not hasattr(response.usage, 'output_tokens'):
                response.usage.output_tokens = response.usage.completion_tokens

            return ModelResponse(
                output=items,
                usage=usage,
                referenceable_id=None,
            )
            
        # Stop active timer and start idle timer when response is complete
        stop_active_timer()
        start_idle_timer()

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """
        Yields a partial message as it is generated, as well as the usage information.
        """
        # Increment the interaction counter for CLI display
        self.interaction_counter += 1
        
        # Stop idle timer and start active timer to track LLM processing time
        stop_idle_timer()
        start_active_timer()
        
        # Check if streaming should be shown in rich panel
        should_show_rich_stream = os.getenv('CAI_STREAM', 'false').lower() == 'true' and not self.disable_rich_streaming
        
        # Create streaming context if needed
        streaming_context = None
        if should_show_rich_stream:
            streaming_context = create_agent_streaming_context(
                agent_name=self.agent_name,
                counter=self.interaction_counter,
                model=str(self.model)
            )
        
        try:
            with generation_span(
                model=str(self.model),
                model_config=dataclasses.asdict(model_settings)
                | {"base_url": str(self._client.base_url)},
                disabled=tracing.is_disabled(),
            ) as span_generation:
                # Prepare messages for consistent token counting
                converted_messages = _Converter.items_to_messages(input)
                if system_instructions:
                    converted_messages.insert(
                        0,
                        {
                            "content": system_instructions,
                            "role": "system",
                        },
                    )
               # --- Add to message_history: user, system prompts ---
                if system_instructions:
                    sys_msg = {
                        "role": "system",
                        "content": system_instructions
                    }
                    add_to_message_history(sys_msg)
                    
                if isinstance(input, str):
                    user_msg = {
                        "role": "user",
                        "content": input
                    }
                    add_to_message_history(user_msg)
                    # Log the user message
                    self.logger.log_user_message(input)
                elif isinstance(input, list):
                    for item in input:
                        if isinstance(item, dict):
                            if item.get("role") == "user":
                                user_msg = {
                                    "role": "user",
                                    "content": item.get("content", "")
                                }
                                add_to_message_history(user_msg)
                                # Log the user message
                                if item.get("content"):
                                    self.logger.log_user_message(item.get("content"))
                # Get token count estimate before API call for consistent counting
                estimated_input_tokens, _ = count_tokens_with_tiktoken(converted_messages)
                
                response, stream = await self._fetch_response(
                    system_instructions,
                    input,
                    model_settings,
                    tools,
                    output_schema,
                    handoffs,
                    span_generation,
                    tracing,
                    stream=True,
                )

                usage: CompletionUsage | None = None
                state = _StreamingState()
                
                # Manual token counting (when API doesn't provide it)
                output_text = ""
                estimated_output_tokens = 0
                
                # Initialize a streaming text accumulator for rich display
                streaming_text_buffer = ""
                # For tool call streaming, accumulate tool_calls to add to message_history at the end
                streamed_tool_calls = []
                
                # Ollama specific: accumulate full content to check for function calls at the end
                # Some Ollama models output the function call as JSON in the text content
                ollama_full_content = ""
                is_ollama = False
                
                model_str = str(self.model).lower()
                is_ollama = self.is_ollama or "ollama" in model_str or ":" in model_str or "qwen" in model_str
                
                # Add visual separation before agent output
                if streaming_context and should_show_rich_stream:
                    # If we're using rich context, we'll add separation through that
                    pass
                else:
                    # Removed clear visual separator to avoid blank lines during streaming
                    pass
                
                try:
                    async for chunk in stream:
                        if not state.started:
                            state.started = True
                            yield ResponseCreatedEvent(
                                response=response,
                                type="response.created",
                            )

                        # The usage is only available in the last chunk
                        if hasattr(chunk, 'usage'):
                            usage = chunk.usage
                        # For Ollama/LiteLLM streams that don't have usage attribute
                        else:
                            usage = None

                        # Handle different stream chunk formats
                        if hasattr(chunk, 'choices') and chunk.choices:
                            choices = chunk.choices
                        elif hasattr(chunk, 'delta') and chunk.delta:
                            # Some providers might return delta directly
                            choices = [{"delta": chunk.delta}]
                        elif isinstance(chunk, dict) and 'choices' in chunk:
                            choices = chunk['choices']
                        # Special handling for Qwen/Ollama chunks 
                        elif isinstance(chunk, dict) and ('content' in chunk or 'function_call' in chunk):
                            # Qwen direct delta format - convert to standard
                            choices = [{"delta": chunk}]
                        else:
                            # Skip chunks that don't contain choice data
                            continue
                        
                        if not choices or len(choices) == 0:
                            continue
                        
                        # Get the delta content
                        delta = None
                        if hasattr(choices[0], 'delta'):
                            delta = choices[0].delta
                        elif isinstance(choices[0], dict) and 'delta' in choices[0]:
                            delta = choices[0]['delta']
                        
                        if not delta:
                            continue

                        # Handle text
                        content = None
                        if hasattr(delta, 'content') and delta.content is not None:
                            content = delta.content
                        elif isinstance(delta, dict) and 'content' in delta and delta['content'] is not None:
                            content = delta['content']
                        
                        if content:
                            # For Ollama, we need to accumulate the full content to check for function calls
                            if is_ollama:
                                ollama_full_content += content
                            
                            # Add to the streaming text buffer
                            streaming_text_buffer += content
                            
                            # Update streaming display if enabled - always do this for text content
                            if streaming_context:
                                update_agent_streaming_content(streaming_context, content)
                            
                            # More accurate token counting for text content
                            output_text += content
                            token_count, _ = count_tokens_with_tiktoken(output_text)
                            estimated_output_tokens = token_count
                            
                            if not state.text_content_index_and_output:
                                # Initialize a content tracker for streaming text
                                state.text_content_index_and_output = (
                                    0 if not state.refusal_content_index_and_output else 1,
                                    ResponseOutputText(
                                        text="",
                                        type="output_text",
                                        annotations=[],
                                    ),
                                )
                                # Start a new assistant message stream
                                assistant_item = ResponseOutputMessage(
                                    id=FAKE_RESPONSES_ID,
                                    content=[],
                                    role="assistant",
                                    type="message",
                                    status="in_progress",
                                )
                                # Notify consumers of the start of a new output message + first content part
                                yield ResponseOutputItemAddedEvent(
                                    item=assistant_item,
                                    output_index=0,
                                    type="response.output_item.added",
                                )
                                yield ResponseContentPartAddedEvent(
                                    content_index=state.text_content_index_and_output[0],
                                    item_id=FAKE_RESPONSES_ID,
                                    output_index=0,
                                    part=ResponseOutputText(
                                        text="",
                                        type="output_text",
                                        annotations=[],
                                    ),
                                    type="response.content_part.added",
                                )
                            # Emit the delta for this segment of content
                            yield ResponseTextDeltaEvent(
                                content_index=state.text_content_index_and_output[0],
                                delta=content,
                                item_id=FAKE_RESPONSES_ID,
                                output_index=0,
                                type="response.output_text.delta",
                            )
                            # Accumulate the text into the response part
                            state.text_content_index_and_output[1].text += content

                        # Handle refusals (model declines to answer)
                        refusal_content = None
                        if hasattr(delta, 'refusal') and delta.refusal:
                            refusal_content = delta.refusal
                        elif isinstance(delta, dict) and 'refusal' in delta and delta['refusal']:
                            refusal_content = delta['refusal']
                        
                        if refusal_content:
                            if not state.refusal_content_index_and_output:
                                # Initialize a content tracker for streaming refusal text
                                state.refusal_content_index_and_output = (
                                    0 if not state.text_content_index_and_output else 1,
                                    ResponseOutputRefusal(refusal="", type="refusal"),
                                )
                                # Start a new assistant message if one doesn't exist yet (in-progress)
                                assistant_item = ResponseOutputMessage(
                                    id=FAKE_RESPONSES_ID,
                                    content=[],
                                    role="assistant",
                                    type="message",
                                    status="in_progress",
                                )
                                # Notify downstream that assistant message + first content part are starting
                                yield ResponseOutputItemAddedEvent(
                                    item=assistant_item,
                                    output_index=0,
                                    type="response.output_item.added",
                                )
                                yield ResponseContentPartAddedEvent(
                                    content_index=state.refusal_content_index_and_output[0],
                                    item_id=FAKE_RESPONSES_ID,
                                    output_index=0,
                                    part=ResponseOutputText(
                                        text="",
                                        type="output_text",
                                        annotations=[],
                                    ),
                                    type="response.content_part.added",
                                )
                            # Emit the delta for this segment of refusal
                            yield ResponseRefusalDeltaEvent(
                                content_index=state.refusal_content_index_and_output[0],
                                delta=refusal_content,
                                item_id=FAKE_RESPONSES_ID,
                                output_index=0,
                                type="response.refusal.delta",
                            )
                            # Accumulate the refusal string in the output part
                            state.refusal_content_index_and_output[1].refusal += refusal_content

                        # Handle tool calls
                        # Because we don't know the name of the function until the end of the stream, we'll
                        # save everything and yield events at the end
                        tool_calls = self._detect_and_format_function_calls(delta)
                        
                        if tool_calls:
                            for tc_delta in tool_calls:
                                tc_index = tc_delta.index if hasattr(tc_delta, 'index') else tc_delta.get('index', 0)
                                if tc_index not in state.function_calls:
                                    state.function_calls[tc_index] = ResponseFunctionToolCall(
                                        id=FAKE_RESPONSES_ID,
                                        arguments="",
                                        name="",
                                        type="function_call",
                                        call_id="",
                                    )
                                
                                tc_function = None
                                if hasattr(tc_delta, 'function'):
                                    tc_function = tc_delta.function
                                elif isinstance(tc_delta, dict) and 'function' in tc_delta:
                                    tc_function = tc_delta['function']
                                    
                                if tc_function:
                                    # Handle both object and dict formats
                                    args = ""
                                    if hasattr(tc_function, 'arguments'):
                                        args = tc_function.arguments or ""
                                    elif isinstance(tc_function, dict) and 'arguments' in tc_function:
                                        args = tc_function.get('arguments', "") or ""
                                        
                                    name = ""
                                    if hasattr(tc_function, 'name'):
                                        name = tc_function.name or ""
                                    elif isinstance(tc_function, dict) and 'name' in tc_function:
                                        name = tc_function.get('name', "") or ""
                                        
                                    state.function_calls[tc_index].arguments += args
                                    state.function_calls[tc_index].name += name
                                
                                # Handle call_id in both formats
                                call_id = ""
                                if hasattr(tc_delta, 'id'):
                                    call_id = tc_delta.id or ""
                                elif isinstance(tc_delta, dict) and 'id' in tc_delta:
                                    call_id = tc_delta.get('id', "") or ""
                                else:
                                    # For Qwen models, generate a predictable ID if none is provided
                                    if state.function_calls[tc_index].name:
                                        # Generate a stable ID from the function name and arguments
                                        call_id = f"call_{hashlib.md5(state.function_calls[tc_index].name.encode()).hexdigest()[:8]}"

                                state.function_calls[tc_index].call_id += call_id

                                # --- Accumulate tool call for message_history ---
                                # Only add if not already present (avoid duplicates in streaming)
                                tool_call_msg = {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": state.function_calls[tc_index].call_id,
                                            "type": "function",
                                            "function": {
                                                "name": state.function_calls[tc_index].name,
                                                "arguments": state.function_calls[tc_index].arguments
                                            }
                                        }
                                    ]
                                }
                                # Only add if not already in streamed_tool_calls
                                if tool_call_msg not in streamed_tool_calls:
                                    streamed_tool_calls.append(tool_call_msg)
                                    add_to_message_history(tool_call_msg)

                except Exception as e:
                    # Ensure streaming context is cleaned up in case of errors
                    if streaming_context:
                        try:
                            finish_agent_streaming(streaming_context, None)
                        except Exception:
                            pass
                    raise e

                # Special handling for Ollama - check if accumulated text contains a valid function call
                if is_ollama and ollama_full_content and len(state.function_calls) == 0:
                    # Look for JSON object that might be a function call
                    try:
                        # Try to extract a JSON object from the content
                        json_start = ollama_full_content.find('{')
                        json_end = ollama_full_content.rfind('}') + 1
                                            
                        if json_start >= 0 and json_end > json_start:
                            json_str = ollama_full_content[json_start:json_end]                        
                            # Try to parse the JSON
                            parsed = json.loads(json_str)
                            
                            # Check if it looks like a function call
                            if ('name' in parsed and 'arguments' in parsed):
                                logger.debug(f"Found valid function call in Ollama output: {json_str}")
                                
                                # Create a tool call ID
                                tool_call_id = f"call_{hashlib.md5((parsed['name'] + str(time.time())).encode()).hexdigest()[:8]}"
                                
                                # Ensure arguments is a valid JSON string
                                arguments_str = ""
                                if isinstance(parsed['arguments'], dict):
                                    # Remove 'ctf' field if it exists
                                    if 'ctf' in parsed['arguments']:
                                        del parsed['arguments']['ctf']
                                    arguments_str = json.dumps(parsed['arguments'])
                                elif isinstance(parsed['arguments'], str):
                                    # If it's already a string, check if it's valid JSON
                                    try:
                                        # Try parsing to validate and remove 'ctf' if present
                                        args_dict = json.loads(parsed['arguments'])
                                        if isinstance(args_dict, dict) and 'ctf' in args_dict:
                                            del args_dict['ctf']
                                        arguments_str = json.dumps(args_dict)
                                    except:
                                        # If not valid JSON, encode it as a JSON string
                                        arguments_str = json.dumps(parsed['arguments'])
                                else:
                                    # For any other type, convert to string and then JSON
                                    arguments_str = json.dumps(str(parsed['arguments']))                            
                                # Add it to our function_calls state
                                state.function_calls[0] = ResponseFunctionToolCall(
                                    id=FAKE_RESPONSES_ID,
                                    arguments=arguments_str,
                                    name=parsed['name'],
                                    type="function_call",
                                    call_id=tool_call_id,
                                )
                                
                                # Display the tool call in CLI
                                from cai.util import cli_print_agent_messages
                                try:
                                    # Create a message-like object to display the function call
                                    tool_msg = type('ToolCallWrapper', (), {
                                        'content': None,
                                        'tool_calls': [
                                            type('ToolCallDetail', (), {
                                                'function': type('FunctionDetail', (), {
                                                    'name': parsed['name'],
                                                    'arguments': arguments_str
                                                }),
                                                'id': tool_call_id,
                                                'type': 'function'
                                            })
                                        ]
                                    })
                                    
                                    # Print the tool call using the CLI utility
                                    cli_print_agent_messages(
                                        agent_name=getattr(self, 'agent_name', 'Agent'),
                                        message=tool_msg,
                                        counter=getattr(self, 'interaction_counter', 0),
                                        model=str(self.model),
                                        debug=False,
                                        interaction_input_tokens=estimated_input_tokens,
                                        interaction_output_tokens=estimated_output_tokens,
                                        interaction_reasoning_tokens=0,  # Not available for Ollama
                                        total_input_tokens=getattr(self, 'total_input_tokens', 0) + estimated_input_tokens,
                                        total_output_tokens=getattr(self, 'total_output_tokens', 0) + estimated_output_tokens,
                                        total_reasoning_tokens=getattr(self, 'total_reasoning_tokens', 0),
                                        interaction_cost=None,
                                        total_cost=None,
                                        tool_output=None  # Will be shown once the tool is executed
                                    )
                                except Exception as e:
                                    logger.error(f"Error displaying tool call in CLI: {e}")
                                
                                # Add to message history
                                tool_call_msg = {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": tool_call_id,
                                            "type": "function",
                                            "function": {
                                                "name": parsed['name'],
                                                "arguments": arguments_str
                                            }
                                        }
                                    ]
                                }
                                
                                streamed_tool_calls.append(tool_call_msg)
                                add_to_message_history(tool_call_msg)
                                
                                logger.debug(f"Added function call: {parsed['name']} with args: {arguments_str}")
                    except Exception as e:
                        pass

                function_call_starting_index = 0
                if state.text_content_index_and_output:
                    function_call_starting_index += 1
                    # Send end event for this content part
                    yield ResponseContentPartDoneEvent(
                        content_index=state.text_content_index_and_output[0],
                        item_id=FAKE_RESPONSES_ID,
                        output_index=0,
                        part=state.text_content_index_and_output[1],
                        type="response.content_part.done",
                    )

                if state.refusal_content_index_and_output:
                    function_call_starting_index += 1
                    # Send end event for this content part
                    yield ResponseContentPartDoneEvent(
                        content_index=state.refusal_content_index_and_output[0],
                        item_id=FAKE_RESPONSES_ID,
                        output_index=0,
                        part=state.refusal_content_index_and_output[1],
                        type="response.content_part.done",
                    )

                # Actually send events for the function calls
                for function_call in state.function_calls.values():
                    # First, a ResponseOutputItemAdded for the function call
                    yield ResponseOutputItemAddedEvent(
                        item=ResponseFunctionToolCall(
                            id=FAKE_RESPONSES_ID,
                            call_id=function_call.call_id,
                            arguments=function_call.arguments,
                            name=function_call.name,
                            type="function_call",
                        ),
                        output_index=function_call_starting_index,
                        type="response.output_item.added",
                    )
                    # Then, yield the args
                    yield ResponseFunctionCallArgumentsDeltaEvent(
                        delta=function_call.arguments,
                        item_id=FAKE_RESPONSES_ID,
                        output_index=function_call_starting_index,
                        type="response.function_call_arguments.delta",
                    )
                    # Finally, the ResponseOutputItemDone
                    yield ResponseOutputItemDoneEvent(
                        item=ResponseFunctionToolCall(
                            id=FAKE_RESPONSES_ID,
                            call_id=function_call.call_id,
                            arguments=function_call.arguments,
                            name=function_call.name,
                            type="function_call",
                        ),
                        output_index=function_call_starting_index,
                        type="response.output_item.done",
                    )

                # Finally, send the Response completed event
                outputs: list[ResponseOutputItem] = []
                if state.text_content_index_and_output or state.refusal_content_index_and_output:
                    assistant_msg = ResponseOutputMessage(
                        id=FAKE_RESPONSES_ID,
                        content=[],
                        role="assistant",
                        type="message",
                        status="completed",
                    )
                    if state.text_content_index_and_output:
                        assistant_msg.content.append(state.text_content_index_and_output[1])
                    if state.refusal_content_index_and_output:
                        assistant_msg.content.append(state.refusal_content_index_and_output[1])
                    outputs.append(assistant_msg)

                    # send a ResponseOutputItemDone for the assistant message
                    yield ResponseOutputItemDoneEvent(
                        item=assistant_msg,
                        output_index=0,
                        type="response.output_item.done",
                    )

                for function_call in state.function_calls.values():
                    outputs.append(function_call)

                final_response = response.model_copy()
                final_response.output = outputs

                # Get final token counts using consistent method
                input_tokens = estimated_input_tokens
                output_tokens = estimated_output_tokens
                
                # Use API token counts if available and reasonable
                if usage and hasattr(usage, 'prompt_tokens') and usage.prompt_tokens > 0:
                    input_tokens = usage.prompt_tokens
                if usage and hasattr(usage, 'completion_tokens') and usage.completion_tokens > 0:
                    output_tokens = usage.completion_tokens
                
                # Create a proper usage object with our token counts
                final_response.usage = CustomResponseUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    output_tokens_details=OutputTokensDetails(
                        reasoning_tokens=usage.completion_tokens_details.reasoning_tokens
                        if usage and hasattr(usage, 'completion_tokens_details') 
                        and usage.completion_tokens_details
                        and hasattr(usage.completion_tokens_details, 'reasoning_tokens')
                        and usage.completion_tokens_details.reasoning_tokens
                        else 0
                    ),
                    input_tokens_details={
                        "prompt_tokens": input_tokens,
                        "cached_tokens": usage.prompt_tokens_details.cached_tokens
                        if usage and hasattr(usage, 'prompt_tokens_details')
                        and usage.prompt_tokens_details
                        and hasattr(usage.prompt_tokens_details, 'cached_tokens')
                        and usage.prompt_tokens_details.cached_tokens
                        else 0
                    },
                )

                yield ResponseCompletedEvent(
                    response=final_response,
                    type="response.completed",
                )
                
                # Update token totals for CLI display
                if final_response.usage:
                    # Always update the total counters with the best available counts
                    self.total_input_tokens += final_response.usage.input_tokens
                    self.total_output_tokens += final_response.usage.output_tokens
                    if (final_response.usage.output_tokens_details and 
                        hasattr(final_response.usage.output_tokens_details, 'reasoning_tokens')):
                        self.total_reasoning_tokens += final_response.usage.output_tokens_details.reasoning_tokens
                
                # Prepare final statistics for display
                interaction_input = final_response.usage.input_tokens if final_response.usage else 0
                interaction_output = final_response.usage.output_tokens if final_response.usage else 0
                total_input = getattr(self, 'total_input_tokens', 0)
                total_output = getattr(self, 'total_output_tokens', 0)
                
                # Calculate costs using the same token counts - ensure model is a string
                model_name = str(self.model)
                interaction_cost = calculate_model_cost(model_name, interaction_input, interaction_output)
                total_cost = calculate_model_cost(model_name, total_input, total_output)
                
                # Explicit conversion to float with fallback to ensure they're never None or 0
                interaction_cost = max(float(interaction_cost if interaction_cost is not None else 0.0), 0.00001)
                total_cost = max(float(total_cost if total_cost is not None else 0.0), 0.00001)
                
                # Store the total cost for future recording
                self.total_cost = total_cost
                
                # Create final stats with explicit type conversion for all values
                final_stats = {
                    "interaction_input_tokens": int(interaction_input),
                    "interaction_output_tokens": int(interaction_output),
                    "interaction_reasoning_tokens": int(
                        final_response.usage.output_tokens_details.reasoning_tokens 
                        if final_response.usage and final_response.usage.output_tokens_details
                        and hasattr(final_response.usage.output_tokens_details, 'reasoning_tokens')
                        else 0
                    ),
                    "total_input_tokens": int(total_input),
                    "total_output_tokens": int(total_output),
                    "total_reasoning_tokens": int(getattr(self, 'total_reasoning_tokens', 0)),
                    "interaction_cost": float(interaction_cost),
                    "total_cost": float(total_cost),
                }
                
                # At the end of streaming, finish the streaming context if we were using it
                if streaming_context:
                    # Create a direct copy of the costs to ensure they remain as floats
                    direct_stats = final_stats.copy()
                    direct_stats["interaction_cost"] = float(interaction_cost)
                    direct_stats["total_cost"] = float(total_cost)
                    # Use the direct copy with guaranteed float costs
                    finish_agent_streaming(streaming_context, direct_stats)
                    
                    # Removed extra newline after streaming completes to avoid blank lines
                    pass

                if tracing.include_data():
                    span_generation.span_data.output = [final_response.model_dump()]

                span_generation.span_data.usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }

                # --- Add assistant tool call(s) to message_history at the end of streaming ---
                for tool_call_msg in streamed_tool_calls:
                    add_to_message_history(tool_call_msg)
                
                # Log the assistant tool call message if any tool calls were collected
                if streamed_tool_calls:
                    tool_calls_list = []
                    for tool_call_msg in streamed_tool_calls:
                        for tool_call in tool_call_msg.get("tool_calls", []):
                            tool_calls_list.append(tool_call)
                    self.logger.log_assistant_message(None, tool_calls_list)
               # If there was only text output, add that as an assistant message
                if (not streamed_tool_calls) and state.text_content_index_and_output and state.text_content_index_and_output[1].text:
                    asst_msg = {
                        "role": "assistant",
                        "content": state.text_content_index_and_output[1].text
                    }
                    add_to_message_history(asst_msg)
                    # Log the assistant message
                    self.logger.log_assistant_message(state.text_content_index_and_output[1].text)
                
                # Log the complete response
                self.logger.rec_training_data(
                    {
                        "model": str(self.model),
                        "messages": converted_messages,
                        "stream": True,
                        "tools": [t.params_json_schema for t in tools] if tools else [],
                        "tool_choice": model_settings.tool_choice
                    },
                    final_response,
                    self.total_cost
                )
                
                # Stop active timer and start idle timer when streaming is complete
                stop_active_timer()
                start_idle_timer()
                
        except Exception as e:
            # Ensure streaming context is cleaned up in case of errors
            if streaming_context:
                try:
                    finish_agent_streaming(streaming_context, None)
                except Exception:
                    pass
                    
            # Stop active timer and start idle timer when streaming errors out
            stop_active_timer()
            start_idle_timer()
            
            raise e

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[True],
    ) -> tuple[Response, AsyncStream[ChatCompletionChunk]]: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[False],
    ) -> ChatCompletion: ...

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool = False,
    ) -> ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]:

        # start by re-fetching self.is_ollama
        self.is_ollama = os.getenv('OLLAMA') is not None and os.getenv('OLLAMA').lower() == 'true'

        converted_messages = _Converter.items_to_messages(input)

        if system_instructions:
            converted_messages.insert(
                0,
                {
                    "content": system_instructions,
                    "role": "system",
                },
            )
        if tracing.include_data():
            span.span_data.input = converted_messages

        parallel_tool_calls = (
            True if model_settings.parallel_tool_calls and tools and len(tools) > 0 else NOT_GIVEN
        )
        tool_choice = _Converter.convert_tool_choice(model_settings.tool_choice)
        response_format = _Converter.convert_response_format(output_schema)
        converted_tools = [ToolConverter.to_openai(tool) for tool in tools] if tools else []

        for handoff in handoffs:
            converted_tools.append(ToolConverter.convert_handoff_tool(handoff))

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            logger.debug(
                f"{json.dumps(converted_messages, indent=2)}\n"
                f"Tools:\n{json.dumps(converted_tools, indent=2)}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice}\n"
                f"Response format: {response_format}\n"
                f"Using OLLAMA: {self.is_ollama}\n"
            )

        # Match the behavior of Responses where store is True when not given
        store = model_settings.store if model_settings.store is not None else True

        # Prepare kwargs for the API call
        kwargs = {
            "model": self.model,
            "messages": converted_messages,
            "tools": converted_tools or NOT_GIVEN,
            "temperature": self._non_null_or_not_given(model_settings.temperature),
            "top_p": self._non_null_or_not_given(model_settings.top_p),
            "frequency_penalty": self._non_null_or_not_given(model_settings.frequency_penalty),
            "presence_penalty": self._non_null_or_not_given(model_settings.presence_penalty),
            "max_tokens": self._non_null_or_not_given(model_settings.max_tokens),
            "tool_choice": tool_choice,
            "response_format": response_format,
            "parallel_tool_calls": parallel_tool_calls,
            "stream": stream,
            "stream_options": {"include_usage": True} if stream else NOT_GIVEN,
            "store": store,
            "extra_headers": _HEADERS,
        }

        # Determine provider based on model string
        model_str = str(self.model).lower()
        
        # Provider-specific adjustments
        if "/" in model_str:
            # Handle provider/model format
            provider = model_str.split("/")[0]
            
            # Apply provider-specific configurations
            if provider == "deepseek":
                litellm.drop_params = True
                kwargs.pop("parallel_tool_calls", None)
                # Remove tool_choice if no tools are specified
                if not converted_tools:
                    kwargs.pop("tool_choice", None)
            elif provider == "claude":
                litellm.drop_params = True
                kwargs.pop("store", None)
                # Remove tool_choice if no tools are specified
                if not converted_tools:
                    kwargs.pop("tool_choice", None)
            elif provider == "gemini":
                kwargs.pop("parallel_tool_calls", None)
                # Add any specific gemini settings if needed
        else:
            # Handle models without provider prefix
            if "claude" in model_str:
                litellm.drop_params = True
                # Remove store parameter which isn't supported by Anthropic
                kwargs.pop("store", None)
                # Remove tool_choice if no tools are specified
                if not converted_tools:
                    kwargs.pop("tool_choice", None)
            elif "gemini" in model_str:
                kwargs.pop("parallel_tool_calls", None)
            elif "qwen" in model_str or ":" in model_str:
                # Handle Ollama-served models with custom formats (e.g., qwen2.5:14b)
                # These typically need the Ollama provider
                litellm.drop_params = True
                kwargs.pop("parallel_tool_calls", None)
                # These models may not support certain parameters
                if not converted_tools:
                    kwargs.pop("tool_choice", None)
                # Don't add custom_llm_provider here to avoid duplication with Ollama provider
                if self.is_ollama:
                    # Clean kwargs for ollama to avoid parameter conflicts
                    for param in ["custom_llm_provider"]:
                        kwargs.pop(param, None)
            elif any(x in model_str for x in ["o1", "o3", "o4"]):
                # Handle OpenAI reasoning models (o1, o3, o4)
                kwargs.pop("parallel_tool_calls", None)
                # Add reasoning effort if provided
                if hasattr(model_settings, "reasoning_effort"):
                    kwargs["reasoning_effort"] = model_settings.reasoning_effort

        
        # Filter out NotGiven values to avoid JSON serialization issues
        filtered_kwargs = {}
        for key, value in kwargs.items():
            if value is not NOT_GIVEN:
                filtered_kwargs[key] = value
        kwargs = filtered_kwargs
        
        try:
            if self.is_ollama:
                return await self._fetch_response_litellm_ollama(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)
            else:
                return await self._fetch_response_litellm_openai(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)
                
        except litellm.exceptions.BadRequestError as e:
            # print(color("BadRequestError encountered: " + str(e), fg="yellow"))
            if "LLM Provider NOT provided" in str(e):
                model_str = str(self.model).lower()
                provider = None
                is_qwen = "qwen" in model_str or ":" in model_str
                
                # Special handling for Qwen models
                if is_qwen:
                    try:
                        # Use the specialized Qwen approach first
                        return await self._fetch_response_litellm_ollama(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)
                    except Exception as qwen_e:
                        print(qwen_e)
                        # If that fails, try our direct OpenAI approach
                        qwen_params = kwargs.copy()
                        qwen_params["api_base"] = get_ollama_api_base()
                        qwen_params["custom_llm_provider"] = "openai"  # Use openai provider
                        
                        # Make sure tools are passed
                        if "tools" in kwargs and kwargs["tools"]:
                            qwen_params["tools"] = kwargs["tools"]
                        if "tool_choice" in kwargs and kwargs["tool_choice"] is not NOT_GIVEN:
                            qwen_params["tool_choice"] = kwargs["tool_choice"]
                        
                        try:
                            if stream:
                                # Streaming case
                                response = Response(
                                    id=FAKE_RESPONSES_ID,
                                    created_at=time.time(),
                                    model=self.model,
                                    object="response",
                                    output=[],
                                    tool_choice="auto" if tool_choice is None or tool_choice == NOT_GIVEN else cast(Literal["auto", "required", "none"], tool_choice),
                                    top_p=model_settings.top_p,
                                    temperature=model_settings.temperature,
                                    tools=[],
                                    parallel_tool_calls=parallel_tool_calls or False,
                                )
                                stream_obj = await litellm.acompletion(**qwen_params)
                                return response, stream_obj
                            else:
                                # Non-streaming case
                                ret = litellm.completion(**qwen_params)
                                return ret
                        except Exception as direct_e:
                            # All approaches failed, log and raise the original error
                            print(f"All Qwen approaches failed. Original error: {str(e)}, Direct error: {str(direct_e)}")
                            raise e
                
                # Try to detect provider from model string
                if "/" in model_str:
                    provider = model_str.split("/")[0]
                
                if provider:
                    # Add provider-specific settings based on detected provider
                    provider_kwargs = kwargs.copy()
                    if provider == "deepseek":
                        provider_kwargs["custom_llm_provider"] = "deepseek"
                    elif provider == "claude" or "claude" in model_str:
                        provider_kwargs["custom_llm_provider"] = "anthropic"
                    elif provider == "gemini":
                        provider_kwargs["custom_llm_provider"] = "gemini"
                    else:
                        # For unknown providers, try ollama as fallback
                        return await self._fetch_response_litellm_ollama(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)
                        
            elif ("An assistant message with 'tool_calls'" in str(e) or
                "`tool_use` blocks must be followed by a user message with `tool_result`" in str(e)):  # noqa: E501 # pylint: disable=C0301
                print(f"Error: {str(e)}")
                # NOTE: EDGE CASE: Report Agent CTRL C error
                #
                # This fix CTRL-C error when message list is incomplete
                # When a tool is not finished but the LLM generates a tool call
                kwargs["messages"] = fix_message_list(kwargs["messages"])
                return await self._fetch_response_litellm_openai(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)

            # this captures an error related to the fact
            # that the messages list contains an empty
            # content position
            elif "expected a string, got null" in str(e):
                print(f"Error: {str(e)}")
                # Fix for null content in messages
                kwargs["messages"] = [
                    msg if msg.get("content") is not None else
                    {**msg, "content": ""} for msg in kwargs["messages"]
                ]
                return await self._fetch_response_litellm_openai(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)

            # Handle Anthropic error for empty text content blocks
            elif ("text content blocks must be non-empty" in str(e) or
                "cache_control cannot be set for empty text blocks" in str(e)):  # noqa

                # Print the error message only once
                print(f"Error: {str(e)}") if not self.empty_content_error_shown else None
                self.empty_content_error_shown = True

                # Fix for empty content in messages for Anthropic models
                kwargs["messages"] = [
                    msg if msg.get("content") not in [None, ""] else
                    {
                        **msg,
                        "content": "Empty content block"
                    } for msg in kwargs["messages"]
                ]
                return await self._fetch_response_litellm_openai(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)
            else:
                raise e
        except litellm.exceptions.RateLimitError as e:
            print("Rate Limit Error:" + str(e))
            # Try to extract retry delay from error response or use default
            retry_delay = 60  # Default delay in seconds
            try:
                # Extract the JSON part from the error message
                json_str = str(e.message).split('VertexAIException - ')[-1]
                error_details = json.loads(json_str)

                retry_info = next(
                    (detail for detail in error_details.get('error', {}).get('details', [])
                        if detail.get('@type') == 'type.googleapis.com/google.rpc.RetryInfo'),
                    None
                )
                if retry_info and 'retryDelay' in retry_info:
                    retry_delay = int(retry_info['retryDelay'].rstrip('s'))
            except Exception as parse_error:
                print(f"Could not parse retry delay, using default: {parse_error}")

            print(f"Waiting {retry_delay} seconds before retrying...")
            time.sleep(retry_delay)

        # fall back to ollama if openai API fails
        except Exception as e:  # pylint: disable=W0718
            print(color("Error encountered: " + str(e), fg="yellow"))
            try:
                return await self._fetch_response_litellm_ollama(kwargs, model_settings, tool_choice, stream, parallel_tool_calls)
            except Exception as execp:  # pylint: disable=W0718
                print("Error: " + str(execp))
                return None

    async def _fetch_response_litellm_openai(
        self,
        kwargs: dict,
        model_settings: ModelSettings,
        tool_choice: ChatCompletionToolChoiceOptionParam | NotGiven,
        stream: bool,
        parallel_tool_calls: bool
    ) -> ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        """Handle standard LiteLLM API calls for OpenAI and compatible models."""
        if stream:
            # Standard LiteLLM handling for streaming
            ret = litellm.completion(**kwargs)
            stream_obj = await litellm.acompletion(**kwargs)

            response = Response(
                id=FAKE_RESPONSES_ID,
                created_at=time.time(),
                model=self.model,
                object="response",
                output=[],
                tool_choice="auto" if tool_choice is None or tool_choice == NOT_GIVEN else cast(Literal["auto", "required", "none"], tool_choice),
                top_p=model_settings.top_p,
                temperature=model_settings.temperature,
                tools=[],
                parallel_tool_calls=parallel_tool_calls or False,
            )
            return response, stream_obj
        else:
            # Standard OpenAI handling for non-streaming
            ret = litellm.completion(**kwargs)
            return ret
            
    async def _fetch_response_litellm_ollama(
        self,
        kwargs: dict,
        model_settings: ModelSettings,
        tool_choice: ChatCompletionToolChoiceOptionParam | NotGiven,
        stream: bool,
        parallel_tool_calls: bool,
        provider="ollama"
    ) -> ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        # Extract only supported parameters for Ollama
        ollama_supported_params = {
            "model": kwargs.get("model", ""),
            "messages": kwargs.get("messages", []),
            "stream": kwargs.get("stream", False)
        }
        
        # Add optional parameters if they exist and are not NOT_GIVEN
        for param in ["temperature", "top_p", "max_tokens"]:
            if param in kwargs and kwargs[param] is not NOT_GIVEN:
                ollama_supported_params[param] = kwargs[param]
        
        # Add extra headers if available
        if "extra_headers" in kwargs:
            ollama_supported_params["extra_headers"] = kwargs["extra_headers"]
            
        # Add tools and tool_choice for compatibility with Qwen
        if "tools" in kwargs and kwargs.get("tools") and kwargs.get("tools") is not NOT_GIVEN:
            ollama_supported_params["tools"] = kwargs.get("tools")
            
        if "tool_choice" in kwargs and kwargs.get("tool_choice") is not NOT_GIVEN:
            ollama_supported_params["tool_choice"] = kwargs.get("tool_choice")

        # Remove None values
        ollama_kwargs = {k: v for k, v in ollama_supported_params.items() if v is not None}
        
        # Check if this is a Qwen model
        model_str = str(self.model).lower()
        is_qwen = "qwen" in model_str
                
        api_base = get_ollama_api_base()
        if "ollama" in provider:
            api_base = api_base.rstrip('/v1')
        # Create response object for streaming
        if stream:
            response = Response(
                id=FAKE_RESPONSES_ID,
                created_at=time.time(),
                model=self.model,
                object="response",
                output=[],
                tool_choice="auto" if tool_choice is None or tool_choice == NOT_GIVEN else 
                    cast(Literal["auto", "required", "none"], tool_choice),
                top_p=model_settings.top_p,
                temperature=model_settings.temperature,
                tools=[],
                parallel_tool_calls=parallel_tool_calls or False,
            )
            # Get streaming response
            stream_obj = await litellm.acompletion(
                **ollama_kwargs,
                api_base=api_base,
                custom_llm_provider=provider,
            )
            return response, stream_obj
        else:

        
            # Get completion response
            return litellm.completion(
                **ollama_kwargs,
                api_base=api_base,
                custom_llm_provider=provider,
            )

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI()
        return self._client

    # Helper function to detect and format function calls from various models
    def _detect_and_format_function_calls(self, delta):
        """
        Helper to detect function calls in different formats and normalize them.
        Handles Qwen specifics where function calls may be formatted differently.
        
        Returns: List of normalized tool calls or None
        """
        # Standard OpenAI-style tool_calls format
        if hasattr(delta, 'tool_calls') and delta.tool_calls:
            return delta.tool_calls
        elif isinstance(delta, dict) and 'tool_calls' in delta and delta['tool_calls']:
            return delta['tool_calls']
        
        # Qwen/Ollama function_call format 
        if isinstance(delta, dict) and 'function_call' in delta:
            function_call = delta['function_call']
            return [{
                'index': 0,
                'id': f"call_{time.time_ns()}",  # Generate a unique ID
                'type': 'function',
                'function': {
                    'name': function_call.get('name', ''),
                    'arguments': function_call.get('arguments', '')
                }
            }]
            
        if isinstance(delta, dict) and 'content' in delta:
            content = delta['content']
            # Try to detect if the content is a JSON string with function call format
            try:
                if isinstance(content, str) and '{' in content and '}' in content:
                    # Try to extract JSON from the content (it might be embedded in text)
                    json_start = content.find('{')
                    json_end = content.rfind('}') + 1
                    if json_start >= 0 and json_end > json_start:
                        json_str = content[json_start:json_end]
                        parsed = json.loads(json_str)
                        if 'name' in parsed and 'arguments' in parsed:
                            # This looks like a function call in JSON format
                            return [{
                                'index': 0,
                                'id': f"call_{time.time_ns()}",  # Generate a unique ID
                                'type': 'function',
                                'function': {
                                    'name': parsed['name'],
                                    'arguments': json.dumps(parsed['arguments']) if isinstance(parsed['arguments'], dict) else parsed['arguments']
                                }
                            }]
            except Exception:
                # If JSON parsing fails, just continue with normal processing
                pass
        
        # Anthropic-style tool_use format
        if hasattr(delta, 'tool_use') and delta.tool_use:
            tool_use = delta.tool_use
            return [{
                'index': 0,
                'id': tool_use.get('id', f"tool_{time.time_ns()}"),
                'type': 'function',
                'function': {
                    'name': tool_use.get('name', ''),
                    'arguments': tool_use.get('input', '{}')
                }
            }]
        elif isinstance(delta, dict) and 'tool_use' in delta and delta['tool_use']:
            tool_use = delta['tool_use']
            return [{
                'index': 0,
                'id': tool_use.get('id', f"tool_{time.time_ns()}"),
                'type': 'function',
                'function': {
                    'name': tool_use.get('name', ''),
                    'arguments': tool_use.get('input', '{}')
                }
            }]
            
        return None


class _Converter:
    @classmethod
    def convert_tool_choice(
        cls, tool_choice: Literal["auto", "required", "none"] | str | None
    ) -> ChatCompletionToolChoiceOptionParam | NotGiven:
        if tool_choice is None:
            return "auto"
        elif tool_choice == "auto":
            return "auto"
        elif tool_choice == "required":
            return "required"
        elif tool_choice == "none":
            return "none"
        else:
            return {
                "type": "function",
                "function": {
                    "name": tool_choice,
                },
            }

    @classmethod
    def convert_response_format(
        cls, final_output_schema: AgentOutputSchema | None
    ) -> ResponseFormat | NotGiven:
        if not final_output_schema or final_output_schema.is_plain_text():
            return None

        return {
            "type": "json_schema",
            "json_schema": {
                "name": "final_output",
                "strict": final_output_schema.strict_json_schema,
                "schema": final_output_schema.json_schema(),
            },
        }

    @classmethod
    def message_to_output_items(cls, message: ChatCompletionMessage) -> list[TResponseOutputItem]:
        items: list[TResponseOutputItem] = []

        message_item = ResponseOutputMessage(
            id=FAKE_RESPONSES_ID,
            content=[],
            role="assistant",
            type="message",
            status="completed",
        )
        if message.content:
            message_item.content.append(
                ResponseOutputText(text=message.content, type="output_text", annotations=[])
            )
        if hasattr(message, 'refusal') and message.refusal:
            message_item.content.append(
                ResponseOutputRefusal(refusal=message.refusal, type="refusal")
            )
        if hasattr(message, 'audio') and message.audio:
            raise AgentsException("Audio is not currently supported")

        if message_item.content:
            items.append(message_item)

        if hasattr(message, 'tool_calls') and message.tool_calls:
            for tool_call in message.tool_calls:
                items.append(
                    ResponseFunctionToolCall(
                        id=FAKE_RESPONSES_ID,
                        call_id=tool_call.id,
                        arguments=tool_call.function.arguments,
                        name=tool_call.function.name,
                        type="function_call",
                    )
                )

        return items

    @classmethod
    def maybe_easy_input_message(cls, item: Any) -> EasyInputMessageParam | None:
        if not isinstance(item, dict):
            return None

        keys = item.keys()
        # EasyInputMessageParam only has these two keys
        if keys != {"content", "role"}:
            return None

        role = item.get("role", None)
        if role not in ("user", "assistant", "system", "developer"):
            return None

        if "content" not in item:
            return None

        return cast(EasyInputMessageParam, item)

    @classmethod
    def maybe_input_message(cls, item: Any) -> Message | None:
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role")
            in (
                "user",
                "system",
                "developer",
            )
        ):
            return cast(Message, item)

        return None

    @classmethod
    def maybe_file_search_call(cls, item: Any) -> ResponseFileSearchToolCallParam | None:
        if isinstance(item, dict) and item.get("type") == "file_search_call":
            return cast(ResponseFileSearchToolCallParam, item)
        return None

    @classmethod
    def maybe_function_tool_call(cls, item: Any) -> ResponseFunctionToolCallParam | None:
        if isinstance(item, dict) and item.get("type") == "function_call":
            return cast(ResponseFunctionToolCallParam, item)
        return None

    @classmethod
    def maybe_function_tool_call_output(
        cls,
        item: Any,
    ) -> FunctionCallOutput | None:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            return cast(FunctionCallOutput, item)
        return None

    @classmethod
    def maybe_item_reference(cls, item: Any) -> ItemReference | None:
        if isinstance(item, dict) and item.get("type") == "item_reference":
            return cast(ItemReference, item)
        return None

    @classmethod
    def maybe_response_output_message(cls, item: Any) -> ResponseOutputMessageParam | None:
        # ResponseOutputMessage is only used for messages with role assistant
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "assistant"
        ):
            return cast(ResponseOutputMessageParam, item)
        return None

    @classmethod
    def extract_text_content(
        cls, content: str | Iterable[ResponseInputContentParam]
    ) -> str | list[ChatCompletionContentPartTextParam]:
        all_content = cls.extract_all_content(content)
        if isinstance(all_content, str):
            return all_content
        out: list[ChatCompletionContentPartTextParam] = []
        for c in all_content:
            if c.get("type") == "text":
                out.append(cast(ChatCompletionContentPartTextParam, c))
        return out

    @classmethod
    def extract_all_content(
        cls, content: str | Iterable[ResponseInputContentParam]
    ) -> str | list[ChatCompletionContentPartParam]:
        if isinstance(content, str):
            return content
        out: list[ChatCompletionContentPartParam] = []

        for c in content:
            if isinstance(c, dict) and c.get("type") == "input_text":
                casted_text_param = cast(ResponseInputTextParam, c)
                out.append(
                    ChatCompletionContentPartTextParam(
                        type="text",
                        text=casted_text_param["text"],
                    )
                )
            elif isinstance(c, dict) and c.get("type") == "input_image":
                casted_image_param = cast(ResponseInputImageParam, c)
                if "image_url" not in casted_image_param or not casted_image_param["image_url"]:
                    raise UserError(
                        f"Only image URLs are supported for input_image {casted_image_param}"
                    )
                out.append(
                    ChatCompletionContentPartImageParam(
                        type="image_url",
                        image_url={
                            "url": casted_image_param["image_url"],
                            "detail": casted_image_param["detail"],
                        },
                    )
                )
            elif isinstance(c, dict) and c.get("type") == "input_file":
                raise UserError(f"File uploads are not supported for chat completions {c}")
            else:
                raise UserError(f"Unknown content: {c}")
        return out

    @classmethod
    def items_to_messages(
        cls,
        items: str | Iterable[TResponseInputItem],
    ) -> list[ChatCompletionMessageParam]:
        """
        Convert a sequence of 'Item' objects into a list of ChatCompletionMessageParam.

        Rules:
        - EasyInputMessage or InputMessage (role=user) => ChatCompletionUserMessageParam
        - EasyInputMessage or InputMessage (role=system) => ChatCompletionSystemMessageParam
        - EasyInputMessage or InputMessage (role=developer) => ChatCompletionDeveloperMessageParam
        - InputMessage (role=assistant) => Start or flush a ChatCompletionAssistantMessageParam
        - response_output_message => Also produces/flushes a ChatCompletionAssistantMessageParam
        - tool calls get attached to the *current* assistant message, or create one if none.
        - tool outputs => ChatCompletionToolMessageParam
        """

        if isinstance(items, str):
            return [
                ChatCompletionUserMessageParam(
                    role="user",
                    content=items,
                )
            ]

        result: list[ChatCompletionMessageParam] = []
        current_assistant_msg: ChatCompletionAssistantMessageParam | None = None

        def flush_assistant_message() -> None:
            nonlocal current_assistant_msg
            if current_assistant_msg is not None:
                # The API doesn't support empty arrays for tool_calls
                if not current_assistant_msg.get("tool_calls"):
                    del current_assistant_msg["tool_calls"]
                result.append(current_assistant_msg)
                current_assistant_msg = None

        def ensure_assistant_message() -> ChatCompletionAssistantMessageParam:
            nonlocal current_assistant_msg
            if current_assistant_msg is None:
                current_assistant_msg = ChatCompletionAssistantMessageParam(role="assistant")
                current_assistant_msg["tool_calls"] = []
            return current_assistant_msg

        for item in items:
            # 1) Check easy input message
            if easy_msg := cls.maybe_easy_input_message(item):
                role = easy_msg["role"]
                content = easy_msg["content"]

                if role == "user":
                    flush_assistant_message()
                    msg_user: ChatCompletionUserMessageParam = {
                        "role": "user",
                        "content": cls.extract_all_content(content),
                    }
                    result.append(msg_user)
                elif role == "system":
                    flush_assistant_message()
                    msg_system: ChatCompletionSystemMessageParam = {
                        "role": "system",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_system)
                elif role == "developer":
                    flush_assistant_message()
                    msg_developer: ChatCompletionDeveloperMessageParam = {
                        "role": "developer",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_developer)
                elif role == "assistant":
                    flush_assistant_message()
                    msg_assistant: ChatCompletionAssistantMessageParam = {
                        "role": "assistant",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_assistant)
                else:
                    raise UserError(f"Unexpected role in easy_input_message: {role}")

            # 2) Check input message
            elif in_msg := cls.maybe_input_message(item):
                role = in_msg["role"]
                content = in_msg["content"]
                flush_assistant_message()

                if role == "user":
                    msg_user = {
                        "role": "user",
                        "content": cls.extract_all_content(content),
                    }
                    result.append(msg_user)
                elif role == "system":
                    msg_system = {
                        "role": "system",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_system)
                elif role == "developer":
                    msg_developer = {
                        "role": "developer",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_developer)
                else:
                    raise UserError(f"Unexpected role in input_message: {role}")

            # 3) response output message => assistant
            elif resp_msg := cls.maybe_response_output_message(item):
                flush_assistant_message()
                new_asst = ChatCompletionAssistantMessageParam(role="assistant")
                contents = resp_msg["content"]

                text_segments = []
                for c in contents:
                    if c["type"] == "output_text":
                        text_segments.append(c["text"])
                    elif c["type"] == "refusal":
                        new_asst["refusal"] = c["refusal"]
                    elif c["type"] == "output_audio":
                        # Can't handle this, b/c chat completions expects an ID which we dont have
                        raise UserError(
                            f"Only audio IDs are supported for chat completions, but got: {c}"
                        )
                    else:
                        raise UserError(f"Unknown content type in ResponseOutputMessage: {c}")

                if text_segments:
                    combined = "\n".join(text_segments)
                    new_asst["content"] = combined

                new_asst["tool_calls"] = []
                current_assistant_msg = new_asst

            # 4) function/file-search calls => attach to assistant
            elif file_search := cls.maybe_file_search_call(item):
                asst = ensure_assistant_message()
                tool_calls = list(asst.get("tool_calls", []))
                new_tool_call = ChatCompletionMessageToolCallParam(
                    id=file_search["id"],
                    type="function",
                    function={
                        "name": "file_search_call",
                        "arguments": json.dumps(
                            {
                                "queries": file_search.get("queries", []),
                                "status": file_search.get("status"),
                            }
                        ),
                    },
                )
                tool_calls.append(new_tool_call)
                asst["tool_calls"] = tool_calls

            elif func_call := cls.maybe_function_tool_call(item):
                asst = ensure_assistant_message()
                tool_calls = list(asst.get("tool_calls", []))
                
                # Save the tool call details for later matching with output
                if not hasattr(cls, 'recent_tool_calls'):
                    cls.recent_tool_calls = {}
                
                # Store the tool call by ID for later reference
                # Also store the current time for execution timing
                import time
                cls.recent_tool_calls[func_call["call_id"]] = {
                    'name': func_call["name"],
                    'arguments': func_call["arguments"],
                    'start_time': time.time(),
                    'execution_info': {
                        'start_time': time.time()
                    }
                }
                
                new_tool_call = ChatCompletionMessageToolCallParam(
                    id=func_call["call_id"],
                    type="function",
                    function={
                        "name": func_call["name"],
                        "arguments": func_call["arguments"],
                    },
                )
                tool_calls.append(new_tool_call)
                asst["tool_calls"] = tool_calls
            
            # 5) function call output => tool message
            elif func_output := cls.maybe_function_tool_call_output(item):
                # Store the output for this call_id
                call_id = func_output["call_id"]
                output_content = func_output["output"]
                
                # Update execution timing if we have the start time
                if hasattr(cls, 'recent_tool_calls') and call_id in cls.recent_tool_calls:
                    tool_call = cls.recent_tool_calls[call_id]
                    if 'start_time' in tool_call:
                        end_time = time.time()
                        tool_execution_time = end_time - tool_call['start_time']
                        
                        # Update the execution info
                        if 'execution_info' in tool_call:
                            tool_call['execution_info']['end_time'] = end_time
                            tool_call['execution_info']['tool_time'] = tool_execution_time
                            
                            # If this is the first tool being executed, record the total time from conversation start
                            if not hasattr(cls, 'conversation_start_time'):
                                cls.conversation_start_time = tool_call['start_time']
                                
                            total_time = end_time - getattr(cls, 'conversation_start_time', tool_call['start_time'])
                            tool_call['execution_info']['total_time'] = total_time
                
                # Store the output so it can be accessed later
                if not hasattr(cls, 'tool_outputs'):
                    cls.tool_outputs = {}
                
                cls.tool_outputs[call_id] = output_content
                
                # Display the tool output immediately with the matched tool call
                from cai.util import cli_print_tool_output
                
                # Check if we're in streaming mode - don't show tool output panel in streaming mode
                is_streaming_enabled = os.environ.get('CAI_STREAM', 'false').lower() == 'true'
                if is_streaming_enabled:
                    # Don't display tool output in streaming mode - it will be handled elsewhere
                    pass  # Just skip the display, but preserve the tool output
                else:
                    # For non-streaming mode, maintain the original behavior
                    # Look up the original tool call to get the name and arguments
                    if hasattr(cls, 'recent_tool_calls') and call_id in cls.recent_tool_calls:
                        tool_call = cls.recent_tool_calls[call_id]
                        tool_name = tool_call.get('name', 'Unknown Tool')
                        tool_args = tool_call.get('arguments', {})
                        execution_info = tool_call.get('execution_info', {})
                        
                        # Get token counts from the OpenAIChatCompletionsModel if available
                        model_instance = None
                        for frame in inspect.stack():
                            if 'self' in frame.frame.f_locals:
                                self_obj = frame.frame.f_locals['self']
                                if isinstance(self_obj, OpenAIChatCompletionsModel):
                                    model_instance = self_obj
                                    break
                        
                        # Always create a token_info dictionary, even if some values are zero
                        token_info = {
                            'interaction_input_tokens': getattr(model_instance, 'interaction_input_tokens', 0),
                            'interaction_output_tokens': getattr(model_instance, 'interaction_output_tokens', 0),
                            'interaction_reasoning_tokens': getattr(model_instance, 'interaction_reasoning_tokens', 0),
                            'total_input_tokens': getattr(model_instance, 'total_input_tokens', 0),
                            'total_output_tokens': getattr(model_instance, 'total_output_tokens', 0),
                            'total_reasoning_tokens': getattr(model_instance, 'total_reasoning_tokens', 0),
                            'model': str(getattr(model_instance, 'model', '')),
                        }
                        
                        # Calculate costs using standard cost model
                        if model_instance and hasattr(model_instance, 'model'):
                            from cai.util import calculate_model_cost
                            model_name = str(model_instance.model)
                            token_info['interaction_cost'] = calculate_model_cost(
                                model_name, 
                                token_info['interaction_input_tokens'], 
                                token_info['interaction_output_tokens']
                            )
                            token_info['total_cost'] = calculate_model_cost(
                                model_name,
                                token_info['total_input_tokens'],
                                token_info['total_output_tokens']
                            )
                        
                        # Use the cli_print_tool_output function with actual token values
                        cli_print_tool_output(
                            tool_name=tool_name, 
                            args=tool_args, 
                            output=output_content, 
                            call_id=call_id,  # Keep call_id for non-streaming mode
                            execution_info=execution_info,
                            token_info=token_info
                        )
                
                # Continue with normal processing
                flush_assistant_message()
                msg: ChatCompletionToolMessageParam = {
                    "role": "tool",
                    "tool_call_id": func_output["call_id"],
                    "content": func_output["output"],
                }
                result.append(msg)

            # 6) item reference => handle or raise
            elif item_ref := cls.maybe_item_reference(item):
                raise UserError(
                    f"Encountered an item_reference, which is not supported: {item_ref}"
                )

            # 7) If we haven't recognized it => fail or ignore
            else:
                raise UserError(f"Unhandled item type or structure: {item}")

        flush_assistant_message()
        return result


class ToolConverter:
    @classmethod
    def to_openai(cls, tool: Tool) -> ChatCompletionToolParam:
        if isinstance(tool, FunctionTool):
            return {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.params_json_schema,
                },
            }

        raise UserError(
            f"Hosted tools are not supported with the ChatCompletions API. FGot tool type: "
            f"{type(tool)}, tool: {tool}"
        )

    @classmethod
    def convert_handoff_tool(cls, handoff: Handoff[Any]) -> ChatCompletionToolParam:
        return {
            "type": "function",
            "function": {
                "name": handoff.tool_name,
                "description": handoff.tool_description,
                "parameters": handoff.input_json_schema,
            },
        }
