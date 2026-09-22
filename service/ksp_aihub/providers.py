"""Bounded protocol adapters. Error diagnostics contain metadata, never model text or keys."""
import json
import socket
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .auth import NoRedirects
from .common import HubError, loads
from .presets import wire_protocol


class ProviderAdapters:
    def __init__(self, broker):
        self.broker = broker
        self.opener = build_opener(NoRedirects())

    @staticmethod
    def _tokens(value):
        return value if type(value) is int and value >= 0 else -1

    def _failure(self, code, profile, result=None, reason="unknown", managed=False):
        usage = (result or {}).get("usage") or {}
        if not isinstance(usage, dict): usage = {}
        details = {"protocol": profile["protocol"], "finishReason": reason,
                   "outputLimit": -1 if managed else profile["maxOutputTokens"],
                   "recoveryLimit": profile.get("recoveryMaxOutputTokens", profile["maxOutputTokens"])}
        for name, value in (("inputTokens", usage.get("input_tokens", usage.get("prompt_tokens"))),
                            ("outputTokens", usage.get("output_tokens", usage.get("completion_tokens")))):
            details[name] = self._tokens(value)
        token_details = usage.get("output_tokens_details", usage.get("completion_tokens_details", {})) or {}
        details["reasoningTokens"] = self._tokens(token_details.get("reasoning_tokens")) if isinstance(token_details, dict) else -1
        descriptions = {
            "output_truncated": "Output was truncated before a complete design was returned. Increase output/recovery budget or reduce reasoning effort and response size.",
            "model_refusal": "The provider refused or filtered this request. Change the request or model; this is not a JSON parsing failure.",
            "model_tools_unsupported": "The model requested tools instead of returning a text design. Tools are not enabled by this gateway.",
            "empty_model_output": "The provider completed without final text. Reasoning content is not a final design.",
            "invalid_json_output": "The completed response was not one valid JSON object. No partial JSON was accepted.",
            "provider_incomplete": "The provider stream ended without explicit completion. No automatic replay was performed.",
            "unsupported_model_output": "The provider returned an unsupported response structure.",
        }
        info = "limit=%s, output_tokens=%s, reasoning_tokens=%s" % (
            "provider-managed" if managed else details["outputLimit"], details["outputTokens"], details["reasoningTokens"])
        raise HubError(code, descriptions[code] + " (" + info + ")", 502, details)

    @staticmethod
    def _json_object(text):
        cleaned = text.strip()
        # Only a single fence around the entire response is tolerated; never extract
        # arbitrary brace fragments or repair a truncated object.
        if cleaned.startswith("```") and cleaned.endswith("```"):
            first, separator, rest = cleaned.partition("\n")
            if separator and first.lower() in ("```", "```json"):
                cleaned = rest[:-3].strip()
        value = loads(cleaned)
        if not isinstance(value, dict): raise ValueError
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def generate(self, profile, messages, output_format):
        profile = dict(profile, protocol=wire_protocol(profile))
        protocol = profile["protocol"]
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if output_format == "json":
            system += "\nReturn exactly one JSON object, without markdown fences or additional text."
        conversation = [dict(m) for m in messages if m["role"] != "system"]
        if not conversation: raise HubError("invalid_request", "At least one user/assistant message is required.")
        headers, secret = self.broker.headers(profile)
        managed = protocol == "responses" and (profile.get("flavor") == "codex" or self.broker.config.credentials[profile["credential"]]["kind"] == "opencode_oauth")
        streaming = profile.get("stream", True)
        effort, thinking = profile.get("reasoningEffort", "provider_default"), profile.get("thinkingMode", "provider_default")
        if protocol == "chat_completions":
            payload = {"model": profile["model"], "messages": [{"role": "system", "content": system}] + conversation,
                       "max_tokens": profile["maxOutputTokens"], "stream": streaming}
            if streaming: payload["stream_options"] = {"include_usage": True}
            if effort != "provider_default": payload["reasoning_effort"] = effort
            if thinking != "provider_default": payload["thinking"] = {"type": thinking}
            if output_format == "json" and profile.get("jsonMode", True): payload["response_format"] = {"type": "json_object"}
            suffix = "/chat/completions"
        elif protocol == "responses":
            payload = {"model": profile["model"], "instructions": system, "store": False, "stream": streaming,
                       "input": [{"role": m["role"], "content": [{"type": "output_text" if m["role"] == "assistant" else "input_text", "text": m["content"]}]} for m in conversation]}
            if effort != "provider_default": payload["reasoning"] = {"effort": effort}
            if not managed:
                payload["max_output_tokens"] = profile["maxOutputTokens"]
                if output_format == "json" and profile.get("jsonMode", True): payload["text"] = {"format": {"type": "json_object"}}
            suffix = "/responses"
        else:
            if effort != "provider_default" or thinking != "provider_default":
                raise HubError("unsupported_generation_settings", "Use provider_default reasoning settings for Messages models.")
            payload = {"model": profile["model"], "system": system, "messages": conversation, "max_tokens": profile["maxOutputTokens"], "stream": streaming}
            suffix = "/messages"
        if streaming: headers["Accept"] = "text/event-stream, application/json"
        request = Request(profile["baseUrl"] + suffix, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"), headers=headers, method="POST")
        deadline = time.monotonic() + profile["timeout"]
        try:
            with self.opener.open(request, timeout=profile["timeout"]) as response:
                if "text/event-stream" in response.headers.get("Content-Type", "").lower():
                    result = self._stream(response, protocol, deadline)
                else:
                    raw = response.read(2000001)
                    if len(raw) > 2000000: raise HubError("provider_response_limit", "Provider response exceeds the size limit.", 502)
                    result = loads(raw)
            if time.monotonic() > deadline: raise TimeoutError
            if not isinstance(result, dict): raise ValueError
            usage = result.get("usage") or {}
            if not isinstance(usage, dict): raise ValueError
            if protocol == "chat_completions":
                choice = result["choices"][0]; message = choice["message"]; reason = choice.get("finish_reason")
                if reason in ("length", "max_tokens"): self._failure("output_truncated", profile, result, reason, managed)
                if reason == "content_filter" or message.get("refusal"): self._failure("model_refusal", profile, result, "refusal", managed)
                if reason in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
                    self._failure("model_tools_unsupported", profile, result, "tools", managed)
                if reason != "stop": self._failure("provider_incomplete", profile, result, "missing_stop", managed)
                text = message.get("content")
                if isinstance(text, list):
                    if any(not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str) for p in text): raise ValueError
                    text = "".join(p["text"] for p in text)
                input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
            elif protocol == "messages":
                reason = result.get("stop_reason")
                if reason == "max_tokens": self._failure("output_truncated", profile, result, reason, managed)
                if reason == "refusal": self._failure("model_refusal", profile, result, reason, managed)
                if reason == "tool_use" or any(p.get("type") == "tool_use" for p in result.get("content", [])):
                    self._failure("model_tools_unsupported", profile, result, "tools", managed)
                if reason not in ("end_turn", "stop_sequence"): self._failure("provider_incomplete", profile, result, "missing_stop", managed)
                text = "".join(p["text"] for p in result.get("content", []) if p.get("type") == "text")
                input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
            else:
                reason = (result.get("incomplete_details") or {}).get("reason")
                if reason == "max_output_tokens": self._failure("output_truncated", profile, result, reason, managed)
                if reason == "content_filter": self._failure("model_refusal", profile, result, reason, managed)
                if result.get("status") != "completed": self._failure("provider_incomplete", profile, result, "missing_completion", managed)
                output = result.get("output", [])
                if any(p.get("type") in ("function_call", "custom_tool_call") for p in output): self._failure("model_tools_unsupported", profile, result, "tools", managed)
                contents = [p for item in output if item.get("type") == "message" and item.get("role") == "assistant" for p in item.get("content", [])]
                if any(p.get("type") == "refusal" for p in contents): self._failure("model_refusal", profile, result, "refusal", managed)
                text = "".join(p.get("text", "") for p in contents if p.get("type") == "output_text")
                input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
            if text is None or text == "": self._failure("empty_model_output", profile, result, "empty_text", managed)
            if not isinstance(text, str) or secret in text: raise ValueError
            json_text = ""
            if output_format == "json":
                try: json_text = self._json_object(text)
                except (HubError, ValueError, TypeError): self._failure("invalid_json_output", profile, result, "invalid_json", managed)
            return {"text": text, "jsonText": json_text, "inputTokens": self._tokens(input_tokens), "outputTokens": self._tokens(output_tokens),
                    "outputLimit": -1 if managed else profile["maxOutputTokens"], "providerManagedOutput": managed}
        except HTTPError as error:
            status = error.code; error.close()
            raise HubError("provider_error", f"Provider returned HTTP {status}; no fallback or retry was performed.", 502, {"httpStatus": status}) from None
        except (TimeoutError, socket.timeout):
            raise HubError("provider_timeout", "Provider exceeded the request timeout. Check its latency and configured timeout; the request was not replayed.", 504) from None
        except URLError as error:
            code = "provider_timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "provider_connection_error"
            raise HubError(code, "Provider connection failed; no fallback or retry was performed.", 502) from None
        except (OSError, HTTPException):
            raise HubError("provider_connection_error", "Provider connection failed; no fallback or retry was performed.", 502) from None
        except HubError as error:
            if error.code == "invalid_json":
                raise HubError("invalid_provider_response", "Provider envelope was not valid JSON.", 502) from None
            raise
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            self._failure("unsupported_model_output", profile, managed=managed)

    @staticmethod
    def _events(response, deadline):
        size, frame_size, events = 0, 0, 0
        data = []
        while True:
            if time.monotonic() >= deadline: raise TimeoutError
            line = response.readline(2000001); size += len(line)
            if size > 64000000 or len(line) > 2000000:
                raise HubError("provider_response_limit", "Provider stream exceeds its wire/frame size limit.", 502)
            if not line: break
            line = line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                frame_size += len(line.encode("utf-8"))
                if frame_size > 2000000: raise HubError("provider_response_limit", "Provider stream frame exceeds the limit.", 502)
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                raw = "\n".join(data); data.clear(); frame_size = 0; events += 1
                if events > 131072: raise HubError("provider_response_limit", "Provider stream has too many events.", 502)
                if raw == "[DONE]": return
                event = loads(raw)
                if not isinstance(event, dict): raise ValueError
                yield event
        if data:
            raw = "\n".join(data)
            if raw != "[DONE]": yield loads(raw)

    def _stream(self, response, protocol, deadline):
        if protocol == "responses": return self._responses_stream(response, deadline=deadline)
        chunks, usage, finish, tools, refusal = [], {}, None, False, False
        stopped = False
        text_bytes = 0
        def append_text(text):
            nonlocal text_bytes
            if not isinstance(text, str): raise ValueError
            text_bytes += len(text.encode("utf-8"))
            if text_bytes > 2000000: raise HubError("provider_response_limit", "Provider text exceeds the decoded output limit.", 502)
            chunks.append(text)
        for event in self._events(response, deadline):
            if "error" in event or event.get("type") == "error":
                raise HubError("provider_stream_error", "Provider reported a stream failure; no replay was performed.", 502)
            if protocol == "chat_completions":
                if isinstance(event.get("usage"), dict): usage.update(event["usage"])
                for choice in event.get("choices", []):
                    if choice.get("index", 0) != 0: continue
                    delta = choice.get("delta") or {}
                    if delta.get("content") is not None:
                        if not isinstance(delta["content"], str): raise ValueError
                        append_text(delta["content"])
                    tools |= bool(delta.get("tool_calls") or delta.get("function_call"))
                    refusal |= bool(delta.get("refusal"))
                    if choice.get("finish_reason") is not None: finish = choice["finish_reason"]
            else:
                kind = event.get("type")
                if kind == "message_start": usage.update(event.get("message", {}).get("usage", {}))
                elif kind == "content_block_start":
                    block = event.get("content_block", {}); tools |= block.get("type") == "tool_use"
                    if block.get("type") == "text": append_text(block.get("text", ""))
                elif kind == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta": append_text(delta["text"])
                elif kind == "message_delta":
                    usage.update(event.get("usage", {})); finish = event.get("delta", {}).get("stop_reason") or finish
                elif kind == "message_stop": stopped = True; break
        text = "".join(chunks)
        if protocol == "chat_completions":
            return {"choices": [{"finish_reason": finish, "message": {"content": text, "tool_calls": tools, "refusal": refusal}}], "usage": usage}
        return {"stop_reason": "tool_use" if tools else finish if stopped else None, "content": [{"type": "text", "text": text}], "usage": usage}

    @staticmethod
    def _responses_stream(response, timeout=None, *, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + timeout
        chunks, texts, items = {}, {}, {}
        refused = False
        delta_bytes = 0
        for event in ProviderAdapters._events(response, deadline):
            kind = event.get("type")
            if kind in ("response.refusal.delta", "response.refusal.done"): refused = True
            if kind in ("error", "response.failed"):
                raise HubError("provider_stream_error", "Provider stream reported failure; no replay was performed.", 502)
            if kind in ("response.output_text.delta", "response.output_text.done", "response.output_item.done"):
                index, content_index = event.get("output_index", 0), event.get("content_index", 0)
                if any(type(n) is not int or not 0 <= n < 128 for n in (index, content_index)): raise ValueError
                key = (index, content_index)
                if kind == "response.output_text.delta":
                    if not isinstance(event.get("delta"), str): raise ValueError
                    delta_bytes += len(event["delta"].encode("utf-8"))
                    if delta_bytes > 2000000: raise HubError("provider_response_limit", "Provider text exceeds the decoded output limit.", 502)
                    chunks.setdefault(key, []).append(event["delta"])
                elif kind == "response.output_text.done":
                    if not isinstance(event.get("text"), str): raise ValueError
                    texts[key] = event["text"]
                else: items[index] = event["item"]
            if kind in ("response.completed", "response.incomplete"):
                completed = event.get("response")
                if not isinstance(completed, dict): raise ValueError
                if not completed.get("output"):
                    output = [item for _, item in sorted(items.items())]
                    if not output:
                        for key, values in chunks.items(): texts.setdefault(key, "".join(values))
                        if texts: output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text} for _, text in sorted(texts.items())]}]
                    completed["output"] = output
                if refused:
                    completed["output"].append({"type": "message", "role": "assistant", "content": [{"type": "refusal"}]})
                if len(json.dumps(completed, ensure_ascii=False).encode("utf-8")) > 2000000:
                    raise HubError("provider_response_limit", "Provider completed response exceeds the output limit.", 502)
                return completed
        raise HubError("provider_incomplete", "Stream ended without explicit completion; no replay was performed.", 502)
