# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import torch

from vllm.config import VllmConfig
from vllm.model_executor.guided_decoding.outlines_logits_processors import (
    OutlinesVocabulary, get_cache, get_vocabulary)
from vllm.sampling_params import SamplingParams
from vllm.transformers_utils.tokenizer_group import init_tokenizer_from_configs
from vllm.utils import LazyLoader
from vllm.v1.structured_output.backend_types import (StructuredOutputBackend,
                                                     StructuredOutputGrammar,
                                                     StructuredOutputOptions)

if TYPE_CHECKING:
    import outlines_core as oc
else:
    oc = LazyLoader("oc", globals(), "outlines_core")

# Python 3.11+ sre_parse and sre_constants
# are deprecated, so we must import them from re
if sys.version_info >= (3, 11):
    from re import _constants as sre_constants
    from re import _parser as sre_parse
else:
    import sre_constants
    import sre_parse


class OutlinesBackend(StructuredOutputBackend):

    def __init__(self, vllm_config: VllmConfig):
        self.vllm_config = vllm_config
        self.vocab_size = vllm_config.model_config.get_vocab_size()

        tokenizer_group = init_tokenizer_from_configs(
            model_config=vllm_config.model_config,
            scheduler_config=vllm_config.scheduler_config,
            lora_config=vllm_config.lora_config)  # type: ignore[arg-type]

        tokenizer = tokenizer_group.get_lora_tokenizer(None)
        self.vocabulary = get_vocabulary(tokenizer)
        self.cache = get_cache()

    def _compile_index(self, regex_string: str,
                       vocabulary: OutlinesVocabulary) -> oc.Index:
        cache_key = f"{vocabulary._hash}_{regex_string}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        index = oc.Index(regex_string, vocabulary.inner)
        self.cache[cache_key] = index

        return index

    def compile_grammar(self, request_type: StructuredOutputOptions,
                        grammar_spec: str) -> StructuredOutputGrammar:
        if request_type == StructuredOutputOptions.JSON:
            regex = oc.json_schema.build_regex_from_schema(grammar_spec)
        elif request_type == StructuredOutputOptions.REGEX:
            regex = grammar_spec
        elif request_type == StructuredOutputOptions.CHOICE:
            choices = ast.literal_eval(grammar_spec)
            choices = [re.escape(c) for c in choices]
            regex = "(" + "|".join(choices) + ")"
        else:
            raise ValueError(
                f"Invalid request type for Outlines backend ({request_type!s})"
            )
        index = self._compile_index(regex, self.vocabulary)
        return OutlinesGrammar(
            vocab_size=self.vocab_size,
            guide=oc.Guide(index,
                           max_rollback=self.vllm_config.speculative_config.
                           num_speculative_tokens))

    def allocate_token_bitmask(self, max_num_seqs: int):
        return torch.full(
            (max_num_seqs, (self.vocab_size + 31) // 32),
            -1,
            dtype=torch.int32,
            pin_memory=torch.cuda.is_available(),
        )

    def destroy(self):
        pass


@dataclass
class OutlinesGrammar(StructuredOutputGrammar):

    vocab_size: int
    guide: oc.Guide = field(hash=False)
    num_processed_tokens: int = field(default_factory=lambda: 0,
                                      repr=False,
                                      hash=False,
                                      init=False)

    # outlines_core signals done on DFA accept; vLLM expects done after EOS.
    # We delay the finished flag by one step so EOS can still be emitted.
    _prev_finished: bool = field(default=False,
                                 init=False,
                                 repr=False,
                                 hash=False)

    def accept_tokens(self, request_id: str, tokens: list[int]) -> bool:
        """Accepts a list of tokens and advances the FSM.

        Returns True if the FSM was advanced successfully.
        Returns False if the FSM failed to advance.
        """
        if self.guide.accepts_tokens(tokens):
            # Advance cannot fail because we checked Guide.accepts_tokens()
            for t in tokens:
                self.guide.advance(t)
            return True
        return False

    def rollback(self, num_tokens: int) -> None:
        self.guide.rollback_state(num_tokens)
        self.num_processed_tokens -= num_tokens

    def validate_tokens(self, tokens: list[int]) -> list[int]:
        accepted: list[int] = []
        for tok in tokens:
            if self.guide.accept_tokens(accepted + [tok]):
                accepted.append(tok)
            else:
                return accepted
        return accepted

    def fill_bitmask(self, bitmask: torch.Tensor, idx: int) -> None:
        mask = bitmask[idx]
        self.guide.write_mask_into(mask.data_ptr(), mask.numel(),
                                   mask.element_size())

    def is_terminated(self) -> bool:
        curr = self.guide.is_finished()
        prev = self._prev_finished
        self._prev_finished = curr
        return prev

    def reset(self):
        self.num_processed_tokens = 0
        self._prev_finished = False
        self.guide.reset()


def validate_structured_output_request_outlines(params: SamplingParams):
    if params.guided_decoding is None:
        return

    gd_params = params.guided_decoding

    if gd_params.regex:
        validate_regex_is_buildable(gd_params.regex)
    elif gd_params.json:
        if isinstance(gd_params.json, str):
            try:
                schema = json.loads(gd_params.json)
            except json.JSONDecodeError as e:
                raise ValueError("Invalid JSON grammar specification.") from e
        else:
            schema = gd_params.json
        pattern = oc.json_schema.build_regex_from_schema(schema)
        validate_regex_is_buildable(pattern)
    elif gd_params.choice:
        choices = [re.escape(str(choice)) for choice in gd_params.choice]
        regex = "(" + "|".join(choices) + ")"
        validate_regex_is_buildable(regex)
    elif gd_params.grammar:
        raise ValueError("Outlines guided decoding backend "
                         "does not support grammar specifications")


def _prefix_needs_context(parsed) -> bool:
    """Return True if there's a look-around/anchor before any consumer."""

    def subpattern_consumes(parsed) -> bool:
        """Return True if subpattern can consume at least one character."""
        tokens = parsed.data if hasattr(parsed, 'data') else parsed
        for ttype, tval in tokens:
            # literal, character class, or dot always consumes
            if ttype in (sre_parse.LITERAL, sre_parse.IN, sre_parse.ANY):
                return True
            # quantified subpattern: check inner pattern
            elif ttype == sre_parse.MAX_REPEAT:
                _, mx, sub = tval
                if mx != 0 and subpattern_consumes(sub):
                    return True
            # alternation: if any branch consumes, the whole does
            elif ttype == sre_parse.BRANCH:
                _, branches = tval
                if any(subpattern_consumes(br) for br in branches):
                    return True
            # grouped subpattern: recurse into its contents
            elif ttype == sre_parse.SUBPATTERN and subpattern_consumes(
                    tval[3]):
                return True
        # No consumers, return False
        return False

    tokens = parsed.data if hasattr(parsed, 'data') else parsed
    for ttype, tval in tokens:
        # Direct anchors or look-around
        if ttype == sre_parse.AT or ttype in (sre_constants.ASSERT,
                                              sre_constants.ASSERT_NOT):
            return True

        # Nested subpattern: check
        if ttype == sre_parse.SUBPATTERN:
            # tval: (group, add_flags, del_flags, subpattern)
            if _prefix_needs_context(tval[3]):
                return True
            if subpattern_consumes(tval[3]):
                return False

        # if any branch has a prefix anchor => True,
        # else if at least one branch consumes => prefix ends => False
        elif ttype == sre_parse.BRANCH:
            saw_consumer = False
            for br in tval[1]:
                if _prefix_needs_context(br):
                    return True
                if subpattern_consumes(br):
                    saw_consumer = True
            if saw_consumer:
                return False

        # Immediate consumer tokens
        elif ttype in (sre_parse.LITERAL, sre_parse.IN, sre_parse.ANY):
            return False

        # if subpattern has anchor => True, if it can consume => stop
        elif ttype == sre_parse.MAX_REPEAT:
            if _prefix_needs_context(tval[2]):
                return True
            if subpattern_consumes(tval[2]):
                return False

    return False


def _check_unsupported(parsed) -> None:
    """Check for regex features unsupported by regex-automata"""
    tokens = parsed.data if hasattr(parsed, 'data') else parsed
    for ttype, tval in tokens:

        # backreference
        if ttype in (sre_parse.GROUPREF, sre_parse.GROUPREF_EXISTS):
            raise ValueError("Backreferences are unsupported.")

        # look-around assertion
        elif ttype in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
            raise ValueError("Look-Around assertion are unsupported.")

        # unicode word boundaries
        elif ttype == sre_parse.AT:
            if tval in (sre_constants.AT_BOUNDARY,
                        sre_constants.AT_NON_BOUNDARY):
                raise ValueError("Unicode word boundaries are unsupported.")

        elif ttype == sre_parse.BRANCH:
            # tval is (None, branches)
            for branch in tval[1]:
                _check_unsupported(branch)

        # tval is (min, max, subpattern)
        elif ttype == sre_parse.MAX_REPEAT:
            _check_unsupported(tval[2])


def validate_regex_is_buildable(pattern: str) -> None:
    """
    Validates that the input regex is not using unsupported features
    of the `regex-automata` crate (outlines_core regex engine), and has a
    universal start state.
    definition of universal start state used can be found at:
    https://docs.rs/regex-automata/latest/regex_automata/dfa/trait.Automaton.html#method.universal_start_state
    """
    try:
        parsed = sre_parse.parse(pattern)

    except sre_constants.error as e:
        raise ValueError(f"Error parsing regex: {e}") from e

    try:
        _check_unsupported(parsed)
    except ValueError as e:
        raise ValueError(
            f"Regex uses unsupported feature for guided decoding: {e}. "
            "Only basic matching constructs are supported—lookarounds, "
            "backreferences, and unicode boundaries are not.") from e

    if _prefix_needs_context(parsed):
        raise ValueError(
            "Regex does not have a anchored universal start state"
            "This means that the Regex uses anchors (^) or look-arounds "
            "in a way which requires context before any token is matched."
            "Guided decoding needs regexes that can match without needing "
            "that context. Try rewriting the pattern without using these "
            f"constructs. Pattern:\n{pattern}")
