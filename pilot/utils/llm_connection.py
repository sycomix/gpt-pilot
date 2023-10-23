import re
import requests
import os
import sys
import time
import json
import tiktoken
import questionary

from typing import List
from jinja2 import Environment, FileSystemLoader

from const.llm import MIN_TOKENS_FOR_GPT_RESPONSE, MAX_GPT_MODEL_TOKENS, MAX_QUESTIONS, END_RESPONSE
from logger.logger import logger
from termcolor import colored
from utils.utils import get_prompt_components, fix_json
from utils.spinner import spinner_start, spinner_stop


def connect_to_llm():
    pass


def get_prompt(prompt_name, data=None):
    if data is None:
        data = {}

    data.update(get_prompt_components())

    logger.debug(f"Getting prompt for {prompt_name}")  # logging here
    # Create a file system loader with the directory of the templates
    file_loader = FileSystemLoader('prompts')

    # Create the Jinja2 environment
    env = Environment(loader=file_loader)

    # Load the template
    template = env.get_template(prompt_name)

    return template.render(data)


def get_tokens_in_messages(messages: List[str]) -> int:
    tokenizer = tiktoken.get_encoding("cl100k_base")  # GPT-4 tokenizer
    tokenized_messages = [tokenizer.encode(message['content']) for message in messages]
    return sum(len(tokens) for tokens in tokenized_messages)

#get endpoint and model name from .ENV file
model = os.getenv('MODEL_NAME')
endpoint = os.getenv('ENDPOINT')

def num_tokens_from_functions(functions, model=model):
    """Return the number of tokens used by a list of functions."""
    encoding = tiktoken.get_encoding("cl100k_base")

    num_tokens = 0
    for function in functions:
        function_tokens = len(encoding.encode(function['name'])) + len(
            encoding.encode(function['description'])
        )
        if 'parameters' in function:
            parameters = function['parameters']
            if 'properties' in parameters:
                for propertiesKey in parameters['properties']:
                    function_tokens += len(encoding.encode(propertiesKey))
                    v = parameters['properties'][propertiesKey]
                    for field in v:
                        if field == 'description':
                            function_tokens += 2
                            function_tokens += len(encoding.encode(v['description']))
                        elif field == 'enum':
                            function_tokens -= 3
                            for o in v['enum']:
                                function_tokens += 3
                                function_tokens += len(encoding.encode(o))
                        elif field == 'type':
                            function_tokens += 2
                            function_tokens += len(encoding.encode(v['type']))
                                            # else:
                                            #     print(f"Warning: not supported field {field}")
                function_tokens += 11

        num_tokens += function_tokens

    num_tokens += 12
    return num_tokens


def create_gpt_chat_completion(messages: List[dict], req_type, min_tokens=MIN_TOKENS_FOR_GPT_RESPONSE,
                               function_calls=None):
    """
    Called from:
      - AgentConvo.send_message() - these calls often have `function_calls`, usually from `pilot/const/function_calls.py`
         - convo.continuous_conversation()
      - prompts.get_additional_info_from_openai()
      - prompts.get_additional_info_from_user() after the user responds to each
            "Please check this message and say what needs to be changed... {message}"
    :param messages: [{ "role": "system"|"assistant"|"user", "content": string }, ... ]
    :param req_type: 'project_description' etc. See common.STEPS
    :param min_tokens: defaults to 600
    :param function_calls: (optional) {'definitions': [{ 'name': str }, ...]}
        see `IMPLEMENT_CHANGES` etc. in `pilot/const/function_calls.py`
    :return: {'text': new_code}
        or if `function_calls` param provided
             {'function_calls': {'name': str, arguments: {...}}}
    """
    gpt_data = {
        'model': os.getenv('MODEL_NAME', 'gpt-4'),
        'n': 1,
        'max_tokens': 4096,
        'temperature': 1,
        'top_p': 1,
        'presence_penalty': 0,
        'frequency_penalty': 0,
        'messages': messages,
        'stream': True
    }

    # delete some keys if using "OpenRouter" API
    if os.getenv('ENDPOINT') == "OPENROUTER":
        keys_to_delete = ['n', 'max_tokens', 'temperature', 'top_p', 'presence_penalty', 'frequency_penalty']
        for key in keys_to_delete:
            if key in gpt_data:
                del gpt_data[key]

    if function_calls is not None:
        # Advise the LLM of the JSON response schema we are expecting
        gpt_data['functions'] = function_calls['definitions']
        if len(function_calls['definitions']) > 1:
            gpt_data['function_call'] = 'auto'
        else:
            gpt_data['function_call'] = {'name': function_calls['definitions'][0]['name']}

    try:
        return stream_gpt_completion(gpt_data, req_type)
    except Exception as e:
        error_message = str(e)

        if "context_length_exceeded" in error_message.lower():
            raise Exception('Too many tokens in the request. Please try to continue the project with some previous development step.')
        print('The request to OpenAI API failed. Here is the error message:')
        print(e)


def delete_last_n_lines(n):
    for _ in range(n):
        # Move the cursor up one line
        sys.stdout.write('\033[F')
        # Clear the current line
        sys.stdout.write('\033[K')


def count_lines_based_on_width(content, width):
    return sum(len(line) // width + 1 for line in content.split('\n'))


def retry_on_exception(func):
    def wrapper(*args, **kwargs):
        while True:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Convert exception to string
                err_str = str(e)

                # If the specific error "context_length_exceeded" is present, simply return without retry
                if "context_length_exceeded" in err_str:
                    raise Exception("context_length_exceeded")
                if "rate_limit_exceeded" in err_str:
                    # Extracting the duration from the error string
                    match = re.search(r"Please try again in (\d+)ms.", err_str)
                    if match:
                        wait_duration = int(match.group(1)) / 1000
                        time.sleep(wait_duration)
                    continue

                print(colored('There was a problem with request to openai API:', 'red'))
                print(err_str)

                user_message = questionary.text(
                    "Do you want to try make the same request again? If yes, just press ENTER. Otherwise, type 'no'.",
                    style=questionary.Style([
                        ('question', 'fg:red'),
                        ('answer', 'fg:orange')
                    ])).ask()

                if user_message != '':
                    return {}

    return wrapper


@retry_on_exception
def stream_gpt_completion(data, req_type):
    """
    Called from create_gpt_chat_completion()
    :param data:
    :param req_type: 'project_description' etc. See common.STEPS
    :return: {'text': str} or {'function_calls': {'name': str, arguments: '{...}'}}
    """
    terminal_width = os.get_terminal_size().columns
    lines_printed = 2
    buffer = ""  # A buffer to accumulate incoming data

    def return_result(result_data, lines_printed):
        if buffer:
            lines_printed += count_lines_based_on_width(buffer, terminal_width)
        logger.info(f'lines printed: {lines_printed} - {terminal_width}')

        delete_last_n_lines(lines_printed)
        return result_data

    # spinner = spinner_start(colored("Waiting for OpenAI API response...", 'yellow'))
    # print(colored("Stream response from OpenAI:", 'yellow'))

    logger.info(f'Request data: {data}')

    # Check if the ENDPOINT is AZURE
    if endpoint == 'AZURE':
        # If yes, get the AZURE_ENDPOINT from .ENV file
        endpoint_url = os.getenv('AZURE_ENDPOINT') + '/openai/deployments/' + model + '/chat/completions?api-version=2023-05-15'
        headers = {'Content-Type': 'application/json', 'api-key':  os.getenv('AZURE_API_KEY')}
    elif endpoint == 'OPENROUTER':
        # If so, send the request to the OpenRouter API endpoint
        headers = {'Content-Type': 'application/json', 'Authorization':  'Bearer ' + os.getenv("OPENROUTER_API_KEY"), 'HTTP-Referer': 'http://localhost:3000', 'X-Title': 'GPT Pilot (LOCAL)'}
        endpoint_url = os.getenv("OPENROUTER_ENDPOINT", 'https://openrouter.ai/api/v1/chat/completions')
    else:
        # If not, send the request to the OpenAI endpoint
        headers = {'Content-Type': 'application/json', 'Authorization':  'Bearer ' + os.getenv("OPENAI_API_KEY")}
        endpoint_url = os.getenv("OPENAI_ENDPOINT", 'https://api.openai.com/v1/chat/completions')

    response = requests.post(
        endpoint_url,
        headers=headers,
        json=data,
        stream=True
    )

    # Log the response status code and message
    logger.info(f'Response status code: {response.status_code}')

    if response.status_code != 200:
        logger.debug(f'problem with request: {response.text}')
        raise Exception(f"API responded with status code: {response.status_code}. Response text: {response.text}")

    gpt_response = ''
    function_calls = {'name': '', 'arguments': ''}

    for line in response.iter_lines():
        # Ignore keep-alive new lines
        if line:
            line = line.decode("utf-8")  # decode the bytes to string

            if line.startswith('data: '):
                line = line[6:]  # remove the 'data: ' prefix

            # Check if the line is "[DONE]" before trying to parse it as JSON
            if line == "[DONE]":
                continue

            try:
                json_line = json.loads(line)

                if len(json_line['choices']) == 0:
                    continue
                
                if 'error' in json_line:
                    logger.error(f'Error in LLM response: {json_line}')
                    raise ValueError(f'Error in LLM response: {json_line["error"]["message"]}')

                if json_line['choices'][0]['finish_reason'] == 'function_call':
                    function_calls['arguments'] = load_data_to_json(function_calls['arguments'])
                    return return_result({'function_calls': function_calls}, lines_printed)

                json_line = json_line['choices'][0]['delta']

            except json.JSONDecodeError:
                logger.error(f'Unable to decode line: {line}')
                continue  # skip to the next line

            # handle the streaming response
            if 'function_call' in json_line:
                if 'name' in json_line['function_call']:
                    function_calls['name'] = json_line['function_call']['name']
                    print(f'Function call: {function_calls["name"]}')

                if 'arguments' in json_line['function_call']:
                    function_calls['arguments'] += json_line['function_call']['arguments']
                    print(json_line['function_call']['arguments'], end='', flush=True)

            if 'content' in json_line:
                content = json_line.get('content')
                if content:
                    buffer += content  # accumulate the data

                    # If you detect a natural breakpoint (e.g., line break or end of a response object), print & count:
                    if buffer.endswith("\n"):  # or some other condition that denotes a breakpoint
                        lines_printed += count_lines_based_on_width(buffer, terminal_width)
                        buffer = ""  # reset the buffer

                    gpt_response += content
                    print(content, end='', flush=True)

    print('\n')
    if function_calls['arguments'] != '':
        logger.info(f'Response via function call: {function_calls["arguments"]}')
        function_calls['arguments'] = load_data_to_json(function_calls['arguments'])
        return return_result({'function_calls': function_calls}, lines_printed)
    logger.info(f'Response message: {gpt_response}')
    new_code = postprocessing(gpt_response, req_type)  # TODO add type dynamically
    return return_result({'text': new_code}, lines_printed)


def postprocessing(gpt_response, req_type):
    return gpt_response


def load_data_to_json(string):
    return json.loads(fix_json(string))
