import time
import re
import concurrent.futures
import json
import os
from datetime import datetime

import gradio as gr

import modules.shared as shared
from modules import chat
import torch
from modules.text_generation import generate_reply_HF, generate_reply_custom
from .llm_web_search import get_webpage_content, langchain_search_duckduckgo, langchain_search_searxng
from .langchain_websearch import LangchainCompressor


params = {
    "display_name": "LLM Web Search",
    "is_tab": True,
    "enable": True,
    "search results per query": 10,
    "langchain similarity score threshold": 0.5,
    "instant answers": True,
    "regular search results": True,
    "search command regex": "",
    "default search command regex": "Search_web: \"(.*)\"",
    "open url command regex": "",
    "default open url command regex": "Open_url: \"(.*)\"",
    "display search results in chat": True,
    "display extracted URL content in chat": True,
    "searxng url": "",
    "cpu only": False
}
langchain_compressor = LangchainCompressor()
update_history = None


def setup():
    """
    Is executed when the extension gets imported.
    :return:
    """
    global params
    try:
        extension_path = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(extension_path, "settings.json"), "r") as f:
            saved_params = json.load(f)
        params.update(saved_params)
    except FileNotFoundError:
        pass
    toggle_extension(params["enable"])


def save_settings():
    global params
    extension_path = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(extension_path, "settings.json"), "w") as f:
        json.dump(params, f)
    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return gr.HTML(f'<font color="green"> Settings were saved at {current_datetime}</font>',
                   visible=True)


def toggle_extension(_enable: bool):
    global langchain_compressor
    if _enable:
        langchain_compressor = LangchainCompressor(device="cpu" if params["cpu only"] else "cuda")
        compressor_model = langchain_compressor.embeddings.client
        compressor_model.to(compressor_model._target_device)
    else:
        if not params["cpu only"]:  # free some VRAM
            del langchain_compressor.embeddings.client
            torch.cuda.empty_cache()
    params.update({"enable": _enable})


def ui():
    """
    Creates custom gradio elements when the UI is launched.
    :return:
    """
    def update_result_type_setting(choice: str):
        if choice == "Instant answers":
            params.update({"instant answers": True})
            params.update({"regular search results": False})
        elif choice == "Regular results":
            params.update({"instant answers": False})
            params.update({"regular search results": True})
        elif choice == "Regular results and instant answers":
            params.update({"instant answers": True})
            params.update({"regular search results": True})

    def update_regex_setting(input_str: str, setting_key: str, error_html_element: gr.component):
        if input_str == "":
            params.update({setting_key: params[f"default {setting_key}"]})
            return {error_html_element: gr.HTML("", visible=False)}
        try:
            compiled = re.compile(input_str)
            if compiled.groups > 1:
                raise re.error(f"Only 1 capturing group allowed in regex, but there are {compiled.groups}.")
            params.update({setting_key: input_str})
            return {error_html_element: gr.HTML("", visible=False)}
        except re.error as e:
            return {error_html_element: gr.HTML(f'<font color="red"> Invalid regex. {str(e).capitalize()}</font>',
                                                visible=True)}

    with gr.Row():
        enable = gr.Checkbox(value=params['enable'], label='Enable LLM web search')
        use_cpu_only = gr.Checkbox(value=params['cpu only'],
                                   label='Run extension on CPU only '
                                         '(Save settings and restart for the change to take effect)')
        with gr.Column():
            save_settings_btn = gr.Button("Save settings")
            saved_success_elem = gr.HTML("", visible=False)

    with gr.Row():
        result_radio = gr.Radio(
            ["Regular results", "Regular results and instant answers"],
            label="What kind of search results should be returned?",
            value="Regular results and instant answers" if (params["regular search results"]
                                                            and params["instant answers"]) else "Regular results"
        )
        with gr.Column():
            search_command_regex = gr.Textbox(label="Search command regex string",
                                              placeholder=params["default search command regex"],
                                              value=params["search command regex"])
            search_command_regex_error_label = gr.HTML("", visible=False)

        with gr.Column():
            open_url_command_regex = gr.Textbox(label="Open URL command regex string",
                                                placeholder=params["default open url command regex"],
                                                value=params["open url command regex"])
            open_url_command_regex_error_label = gr.HTML("", visible=False)

        with gr.Column():
            show_results = gr.Checkbox(value=params['display search results in chat'],
                                       label='Display search results in chat')
            show_url_content = gr.Checkbox(value=params['display extracted URL content in chat'],
                                           label='Display extracted URL content in chat')

    with gr.Accordion("Advanced settings", open=False):
        gr.Markdown("**Note: Changing these might result in DuckDuckGo rate limiting or the LM being overwhelmed**")
        num_search_results = gr.Number(label="Max. search results per query", minimum=1, maximum=100,
                                       value=params["search results per query"], precision=0)
        langchain_similarity_threshold = gr.Number(label="Langchain Similarity Score Threshold", minimum=0., maximum=1.,
                                                   value=params["langchain similarity score threshold"])
    with gr.Row():
        searxng_url = gr.Textbox(label="SearXNG URL",
                                 value=params["searxng url"])

    # Event functions to update the parameters in the backend
    enable.change(toggle_extension, enable, None)
    use_cpu_only.change(lambda x: params.update({"cpu only": x}), use_cpu_only, None)
    save_settings_btn.click(save_settings, None, [saved_success_elem])
    num_search_results.change(lambda x: params.update({"search results per query": x}), num_search_results, None)
    langchain_similarity_threshold.change(lambda x: params.update({"langchain similarity score threshold": x}),
                                          langchain_similarity_threshold, None)
    result_radio.change(update_result_type_setting, result_radio, None)

    search_command_regex.change(lambda x: update_regex_setting(x, "search command regex",
                                                               search_command_regex_error_label),
                                search_command_regex, search_command_regex_error_label)

    open_url_command_regex.change(lambda x: update_regex_setting(x, "open url command regex",
                                                                 open_url_command_regex_error_label),
                                  open_url_command_regex, open_url_command_regex_error_label)

    show_results.change(lambda x: params.update({"display search results in chat": x}), show_results, None)
    show_url_content.change(lambda x: params.update({"display extracted URL content in chat": x}), show_url_content,
                            None)
    searxng_url.change(lambda x: params.update({"searxng url": x}), searxng_url, None)


def custom_generate_reply(question, original_question, seed, state, stopping_strings, is_chat):
    """
    Overrides the main text generation function.
    :return:
    """
    global update_history
    if shared.model.__class__.__name__ in ['LlamaCppModel', 'RWKVModel', 'ExllamaModel', 'Exllamav2Model',
                                           'CtransformersModel']:
        generate_func = generate_reply_custom
    else:
        generate_func = generate_reply_HF

    if not params['enable']:
        for reply in generate_func(question, original_question, seed, state, stopping_strings, is_chat=is_chat):
            yield reply
        return

    web_search = False
    read_webpage = False
    future_to_search_term = {}
    future_to_url = {}
    matched_patterns = {}
    max_search_results = int(params["search results per query"])
    instant_answers = params["instant answers"]
    #regular_search_results = params["regular search results"]
    similarity_score_threshold = params["langchain similarity score threshold"]
    search_command_regex = params["search command regex"]
    open_url_command_regex = params["open url command regex"]
    searxng_url = params["searxng url"]
    display_search_results = params["display search results in chat"]
    display_webpage_content = params["display extracted URL content in chat"]

    if search_command_regex == "":
        search_command_regex = params["default search command regex"]
    if open_url_command_regex == "":
        open_url_command_regex = params["default open url command regex"]

    compiled_search_command_regex = re.compile(search_command_regex)
    compiled_open_url_command_regex = re.compile(open_url_command_regex)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for reply in generate_func(question, original_question, seed, state, stopping_strings, is_chat=is_chat):

            search_re_match = compiled_search_command_regex.search(reply)
            if search_re_match is not None:
                matched_pattern = search_re_match.group(0)
                if matched_patterns.get(matched_pattern):
                    continue
                web_search = True
                matched_patterns[matched_pattern] = True
                search_term = search_re_match.group(1)
                print(f"LLM_Web_search | Searching for {search_term}...")
                if searxng_url == "":
                    future_to_search_term[executor.submit(langchain_search_duckduckgo,
                                                          search_term,
                                                          langchain_compressor,
                                                          max_search_results,
                                                          similarity_score_threshold,
                                                          instant_answers)] = search_term
                else:
                    future_to_search_term[executor.submit(langchain_search_searxng,
                                                          search_term,
                                                          searxng_url,
                                                          langchain_compressor,
                                                          max_search_results,
                                                          similarity_score_threshold)] = search_term

            search_re_match = compiled_open_url_command_regex.search(reply)
            if search_re_match is not None:
                matched_pattern = search_re_match.group(0)
                if matched_patterns.get(matched_pattern):
                    continue
                read_webpage = True
                matched_patterns[matched_pattern] = True
                url = search_re_match.group(1)
                print(f"LLM_Web_search | Reading {url}...")
                future_to_url[executor.submit(get_webpage_content, url)] = url

            # Stop model if either command has been detected in the output
            if (re.search(search_command_regex, reply) is not None
                    or re.search(open_url_command_regex, reply) is not None):
                yield reply
                break
            yield reply

        original_model_reply = reply

        if web_search:
            reply += "\n```"
            reply += "\nSearch tool:\n"
            if display_search_results:
                yield reply
                time.sleep(0.041666666666666664)
            search_result_str = ""
            for i, future in enumerate(concurrent.futures.as_completed(future_to_search_term)):
                search_term = future_to_search_term[future]
                try:
                    data = future.result()
                except Exception as exc:
                    exception_message = str(exc)
                    reply += f"The search tool encountered an error: {exception_message}"
                    print(f'LLM_Web_search | {search_term} generated an exception: {exception_message}')
                else:
                    search_result_str += data
                    reply += data
                    if display_search_results:
                        yield reply
                        time.sleep(0.041666666666666664)
            if search_result_str == "":
                reply += f"\nThe search tool did not return any results."
            reply += "```"
            if display_search_results:
                yield reply
        elif read_webpage:
            reply += "\n```"
            reply += "\nURL opener tool:\n"
            if display_webpage_content:
                yield reply
                time.sleep(0.041666666666666664)
            for i, future in enumerate(concurrent.futures.as_completed(future_to_url)):
                url = future_to_url[future]
                try:
                    data = future.result()
                except Exception as exc:
                    reply += f"Couldn't open {url}. Error message: {str(exc)}"
                    print(f'LLM_Web_search | {url} generated an exception: {str(exc)}')
                else:
                    reply += f"\nText content of {url}:\n"
                    reply += data
                    if display_webpage_content:
                        yield reply
                        time.sleep(0.041666666666666664)
            reply += "```\n"
            if display_webpage_content:
                yield reply

    substring_dict = chat.get_turn_substrings(state, instruct=True)
    if web_search or read_webpage:
        display_results = (web_search and display_search_results or
                           read_webpage and display_webpage_content)
        # Add results to context and continue model output
        new_question = f"{question}{reply}\n\n{substring_dict['bot_turn_stripped']}{state['name2']}:\n"
        new_reply = ""
        for new_reply in generate_func(new_question, new_question, seed, state,
                                       stopping_strings, is_chat=is_chat):
            if display_results:
                yield f"{reply}\n{new_reply}"
            else:
                yield f"{original_model_reply}\n{new_reply}"

        if not display_results:
            update_history = [state["textbox"], f"{reply}\n{new_reply}"]


def output_modifier(string, state, is_chat=False):
    """
    Modifies the output string before it is presented in the UI. In chat mode,
    it is applied to the bot's reply. Otherwise, it is applied to the entire
    output.
    :param string:
    :param state:
    :param is_chat:
    :return:
    """
    return string


def custom_css():
    """
    Returns custom CSS as a string. It is applied whenever the web UI is loaded.
    :return:
    """
    return ''


def custom_js():
    """
    Returns custom javascript as a string. It is applied whenever the web UI is
    loaded.
    :return:
    """
    return ''


def chat_input_modifier(text, visible_text, state):
    """
    Modifies both the visible and internal inputs in chat mode. Can be used to
    hijack the chat input with custom content.
    :param text:
    :param visible_text:
    :param state:
    :return:
    """
    return text, visible_text


def state_modifier(state):
    """
    Modifies the dictionary containing the UI input parameters before it is
    used by the text generation functions.
    :param state:
    :return:
    """
    return state


def history_modifier(history):
    """
    Modifies the chat history before the text generation in chat mode begins.
    :param history:
    :return:
    """
    global update_history
    if update_history:
        history["internal"].append(update_history)
        update_history = None
    return history
