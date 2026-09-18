# LeadGate evaluation report

Phase 5 result: what was tested, how it was scored, what changed along the way, and
what still doesn't work. All numbers below come from `eval/metrics.py`'s live output
against `eval/results/cars_results.json` (45 cases) and
`eval/results/real_estate_results.json` (48 cases), re-run and re-checked on
2026-09-18 while writing this report. Nothing here is rounded up or estimated.

## Synthetic data disclosure

**Every test case in this evaluation is hand-authored, not real customer data.**
`eval/datasets/cars_test_set.json` and `eval/datasets/real_estate_test_set.json`
each carry a `dataset_info.disclosure` field that says so directly:

> HAND-AUTHORED SYNTHETIC TEST DATA. Every "message" in this file was written by a
> human/agent for evaluation purposes. None of it is real customer data, real
> conversation transcripts, or real leads. Do not treat any message, name, email, or
> phone number below as belonging to a real person.

The messages were written to exercise specific categories (clear queries, ambiguous
phrasing, genuine no-match cases, out-of-scope requests, and lead-creation intents),
with expected tool calls and outcomes checked by hand against the real seeded
catalog data (`data/cars_raw.csv`, `data/real_estate_raw.csv`, 350 rows each). The
catalog rows themselves are real (drawn from public used-car and real-estate listing
data used to seed the demo Odoo instance), but the conversational messages, the
labels, and the fictional buyer names, emails, and phone numbers in the create-lead
cases are entirely invented for testing. This is a portfolio evaluation harness,
not a production audit of real customer interactions.

## Methodology

The engine is a domain-agnostic lead-qualification agent (two configured domains:
car sales and real estate) that takes a customer message, decides whether to call a
search tool or a lead-creation tool, and replies in natural language. Each domain
has a hand-labeled test set with an expected tool call, expected extracted
arguments, and an expected outcome per case.

`eval/run_eval.py` sends every case through the real engine, the actual LLM
(OpenRouter's free-tier deepseek/deepseek-v4-flash-0731:free), the actual Odoo
adapters, the actual catalog data, end to end, and records what the engine
actually did. There is no mocking of the model or the data layer anywhere in this
run; case counts, tool-call arguments, and latencies in the results files are the
real values from real API calls.

`eval/metrics.py` then scores those recorded transcripts against the dataset's
labels on four axes:

- Tool selection accuracy: did the engine call the expected tool (or correctly
  call none, for out-of-scope messages)?
- Slot extraction accuracy: for cases where a tool call was expected and made,
  did the extracted arguments match the expected ones? This metric only scores
  cases where the expected tool actually applies and was actually called; a case
  where the wrong tool was chosen is excluded from this metric (its failure already
  shows up in tool selection) rather than silently counted as correct.
- Grounding rate: reported here as 1 minus the hallucination rate, the
  fraction of replies whose factual claims (prices, listing counts, specific
  features) are actually backed by real tool-output data, not invented. A rate of
  100% means the hallucination audit flagged zero cases across the run.
- Task completion rate: did the case reach the outcome the dataset says a
  correctly-behaving agent should reach (the right tool, the right result, or the
  right refusal), end to end?

Final live numbers (.venv/Scripts/python.exe eval/metrics.py, 93 cases total):

| Domain | n | Tool selection | Slot extraction | Grounding rate | Task completion |
|---|---|---|---|---|---|
| Cars | 45 | 100.00% | 98.68% (31 applicable cases) | 100.00% | 97.78% (44/45) |
| Real estate | 48 | 97.92% | 98.33% (32 applicable cases) | 100.00% | 95.83% (46/48) |

See `eval/charts/final_metrics_by_domain.png`.

## The road to these numbers

These weren't the first numbers this eval produced, and the gap between the first
run and this one is worth explaining honestly rather than skipping to the good
part.

Round one (Task 5.2/5.3) put task completion at 84.44% for cars and 79.17% for
real estate. Real estate's slot extraction accuracy showed 100.00% in that run, but
that number was misleading: the metric silently excluded all 8 create_lead cases
from scoring, because their tool selection had already failed, so there was nothing
to grade slots against. A perfect-looking score was hiding a real failure sitting
one metric over.

Digging into why so many create_lead cases were failing surfaced the actual root
cause: the engine had no system prompt at all. Nothing told the model when a
message like "I want to buy this one, here's my number" should end the search and
create a CRM lead instead of triggering another search call. Every one of those
buying-signal messages was met with more searching. That accounted for 5 of the
cars failures and 8 of the 9 known real-estate failures on its own.

Adding a domain-agnostic system prompt with an explicit decision rule fixed most of
that. It also surfaced a second, separate problem while testing: when asked about a
vehicle category the search schema simply has no field for, the model started
writing fabricated tool-output text, invented trucks with invented prices, dressed
up to look like real search results. An anti-fabrication instruction was added to
the same prompt to stop that.

Investigating the failures also turned up four real bugs in the metric itself,
not the engine:

1. The create_lead dataset case's "name" field was being graded by exact string
   match, even though Task 5.1's own dataset documentation says that field is an
   agent-composed title, not a literal fact to reproduce word for word.
2. The "notes" field had the same problem: free text the agent composes, graded as
   if it should match verbatim.
3. The hallucination check's money-parsing regex only recognized "k" shorthand
   ($40k), not "M" ($1.279M), so a reply that correctly cited a real $1,279,000
   listing as "$1.279M" was being flagged as an ungrounded, invented figure.
4. A suggestion-question false positive: clarifying phrases like "did you have a
   price range in mind?" tripped a filter meant for something else.

Each one was checked against the real transcript before being treated as a bug,
not just assumed. After every fix, the full 93-case suite was re-run live end to
end again, not just the cases that had previously failed, to make sure nothing
regressed elsewhere. That gives the final numbers in the table above.

See `eval/charts/task_completion_before_after.png` for the before/after task
completion comparison.

### Independent check on the fixes

Four of the fixes touched the metric itself, and self-grading your own grader is
an easy way to accidentally cook the numbers. So the project controller carried
out this round of fixes directly, instead of delegating to an implementer
sub-agent, and then commissioned an independent adversarial review specifically
to check for metric-gaming. That review built its own adversarial test inputs
rather than reusing the existing dataset and concluded the improvement was real:
the fixes correct genuine defects rather than relaxing the metric to fit the
engine's behavior. The same review also surfaced a real, currently unexploited
weakness, described below.

## Weakest points, stated plainly

Two limitations are documented and left unfixed on purpose, and one weakness in the
metric itself remains open.

No price-minimum parameter. The search tool schema only accepts a price_max
argument. A message like "anything over $400,000?" has no correct way to be
expressed through the current tool schema; the agent's only real option is to pick
an arbitrarily high price_max as a workaround, which is not the same thing as
actually filtering on a minimum. This is a genuine feature gap, not a model or
prompt problem, and fixing it would mean adding a price_min parameter to both
adapters and to the Odoo domain construction, which was judged out of scope for
this round.

Residual tool-selection non-determinism. One case in the real-estate suite (case
42) still shows the free-tier LLM choosing the wrong tool inconsistently across
runs. Roughly one case in nine has shown this kind of variance previously. It is
inherent variance in a free-tier model's output, and prompt wording alone has not
fully eliminated it.

Negation-blind text matching. The independent adversarial review flagged that the
metric's free-text matcher (used for fields like "notes") checks for word overlap
without checking for negation. In principle, a reply containing "not a cash buyer"
could match against an expected value like "cash buyer" on word overlap alone.
Across all 93 real cases in this run, every actual notes value is a non-negated
paraphrase, so this weakness was not triggered here, but it is real, unfixed, and
would need attention before trusting this matcher on a larger or more adversarial
dataset.

None of these three are cosmetic. They are the actual remaining gaps between "this
eval scores well" and "this system is complete," and they are recorded here
instead of smoothed into the numbers above.

## Files

- `eval/charts/final_metrics_by_domain.png`: the four metrics, both domains, final run.
- `eval/charts/task_completion_before_after.png`: task completion rate before and after the system-prompt fix.
- `eval/results/cars_results.json`, `eval/results/real_estate_results.json`: raw per-case transcripts these numbers are computed from.
- `eval/metrics.py`: the scoring script; running it reproduces every number in this report.
- `eval/datasets/cars_test_set.json`, `eval/datasets/real_estate_test_set.json`: the hand-authored test cases and their disclosure/limitations metadata.
