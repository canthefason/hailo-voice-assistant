---
name: voice-pr-flow
description: Workflow rule for PRs that change the Hailo Voice Assistant proxy, prompt, normaliser, or any voice-pipeline behaviour. Use whenever creating a PR that touches `hailo_ollama_proxy/proxy.py`, the injected system prompt, the entity allowlist, the request/response sanitisers, or `voice-test.sh`. Each such PR must be live-tested against the deployed image on the Pi (rpi.local) before the PR body is finalised, and the description must include a side-by-side results table comparing this PR's behaviour against the most recent released `v1.0.x` baseline. Do not skip steps — the model is stochastic and only live tests catch regressions.
---

# voice-pr-flow

The standard sequence for a Hailo Voice Assistant proxy / prompt PR. Every step
is here because skipping it has caused a regression at least once. Run the
steps in order; do not batch the live test until after the build is verified
green.

## 0. Pre-work — sync local main

The local `main` often diverges from `origin/main` because PRs are merged via
squash on GitHub. Before starting a new branch:

```bash
git fetch origin
git checkout main
git reset --hard origin/main   # safe: feature commits are preserved in the squashed origin commit
```

If you skip this, your branch base will include duplicate pre-squash commits
and `git pull` will hit a merge conflict on `proxy.py` / `CHANGELOG.md` later.

## 1. Branch off main

```bash
git checkout -b feat/<descriptive-name>     # use feat/ for additions, fix/ for repairs
```

## 2. Implement the change

Refer to `CLAUDE.md` (repo root) before touching the proxy. Specifically:
- The five request-side layers and their order are load-bearing — adding
  another layer means picking the right slot.
- The `repair-before-reject` taxonomy lists the specific qwen2.5:1.5b output
  bugs the proxy normalises. New observed failure modes belong in
  `_normalize_brightness` / `_coalesce_list_items` / `_clean_entity_id`.
- Silent-reject vs spoken behaviour: if a model reply cannot be made into a
  valid tool call, it must be blanked to `""` (never read aloud as JSON or HA
  errors).
- Never reorder the injected example block without live-testing — the round-3
  experiment in PR #17 regressed dim accuracy from 6/8 to 3/8 in spite of unit
  tests passing.

## 3. Sanity-check locally

For any parser/normaliser change, embed targeted cases against
**captured live-test outputs** (the model's actual prior failure mode), not
synthetic strings:

```bash
python3 - <<'PY'
import sys, importlib.util, json
sys.argv = ['proxy.py']
spec = importlib.util.spec_from_file_location('proxy', 'hailo_ollama_proxy/proxy.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

cases = [('label', 'raw model output as captured from logs', expected_value)]
for label, raw, expected in cases:
    result = m._try_parse_tool_call(raw)
    # assertions here
PY
```

Unit tests pass ≠ live test passes. They catch _regressions in things you
already understood_; they cannot catch new ways the model misbehaves on your
new prompt. Treat them as a fast precheck, not a substitute for step 8.

## 4. Update `CHANGELOG.md`

Add entries under `## [Unreleased]`. Use the structure already established by
prior entries — `### Added`, `### Changed`, `### Fixed`. Be specific about the
failure mode the change addresses (e.g. _"qwen2.5:1.5b at temperature=0.1
deterministically picks `light.0x001788010315dcf2` for office-lights commands"_)
so future readers understand the why.

## 5. Commit + push the branch

```bash
git add hailo_ollama_proxy/proxy.py CHANGELOG.md  # any other touched files
git commit                                         # multi-line message: what + why + caveats
git push -u origin feat/<name>
```

The first push triggers `.github/workflows/build-and-push.yml` which builds
both `canthefason/hailo-ollama:<short-sha>` and
`canthefason/wyoming-hailo-whisper:<short-sha>` for `linux/arm64`.

## 6. Wait for the GHA build

Poll until the build completes — do not skip ahead:

```bash
until [ "$(gh run list --branch feat/<name> --event push --limit 1 \
            --json status --jq '.[0].status')" = "completed" ]; do sleep 20; done
gh run list --branch feat/<name> --event push --limit 1 \
    --json conclusion --jq '.[0].conclusion'   # must print "success"
```

If the build fails, fix it and push again. **Do not deploy a stale image —
that's how we previously chased phantom regressions.**

## 7. Deploy to the Pi

```bash
SHA=$(git rev-parse --short HEAD)
ssh hailo-pi "sed -i.bak 's|canthefason/hailo-ollama:[a-zA-Z0-9._-]*|canthefason/hailo-ollama:$SHA|' ~/homeassistant/compose.yaml \
    && cd ~/homeassistant \
    && docker compose pull hailo-ollama \
    && docker compose up -d hailo-ollama \
    && sleep 4 \
    && docker ps --filter name=hailo-ollama --format '{{.Image}} {{.Status}}'"
```

Last line must show your new SHA, not the old one. Check the proxy startup log
for the active config:

```bash
ssh hailo-pi 'docker logs hailo-ollama 2>&1 | grep "listening on"'
```

## 8. Live-test against the deployed image

```bash
export HOME_ASSISTANT="<HA bearer token>"
bash voice-test.sh --log                         # default 8-command suite
# or, for a focused change:
bash voice-test.sh "<phrasing under test>" "<related phrasing>"
```

The default suite covers the matrix that has revealed every prior regression
(dim/set/at-N%, brighter, darker, plain on/off). `--log` prints the proxy
markers (validation failures, suppressions, retries, entity-allowlist
rejections) so you can tell *why* a `⊘` happened.

## 9. Open the PR

Use the `pr-description` skill for the body shape. **The test plan must
include**:

- The deployed image SHA you tested.
- A side-by-side table comparing each command in the default suite to the
  most recent released `v1.0.x` baseline. Use ✅ / ⊘ / ⚠ / ❌ verdicts and
  the spoken reply or failure mode in plain text.
- Wall-clock duration per command if the change has latency implications
  (retries, extra inference, prompt length).
- Relevant proxy log markers in a fenced block (e.g. the
  `[proxy] retrying once with temperature=0.7` line for retry PRs).

```bash
gh pr create --base main --head feat/<name> --title "..." --body "..."
```

## 10. Restore the Pi if the PR is not merged immediately

The deployed image stays on the Pi after testing. If you are not merging
right away, restore the released `:1.0.X` image so the Pi runs known-good
code in the meantime:

```bash
ssh hailo-pi "sed -i.bak 's|canthefason/hailo-ollama:[a-zA-Z0-9._-]*|canthefason/hailo-ollama:<latest-released-version>|' ~/homeassistant/compose.yaml \
    && cd ~/homeassistant && docker compose pull hailo-ollama && docker compose up -d hailo-ollama"
```

## 11. Post-merge release flow

Once the PR is squash-merged to main:

```bash
git checkout main
git fetch origin && git reset --hard origin/main
git push origin --delete feat/<name>          # remote branch cleanup
git branch -D feat/<name>                     # local
bash bump-version.sh patch                    # 1.0.X → 1.0.X+1 — minor for substantive features
# manually edit CHANGELOG.md: rename [Unreleased] → [<new>] with today's date,
# update the bottom comparison links, leave a fresh empty [Unreleased] block.
git add VERSION CHANGELOG.md
git commit -m "chore: release v<new>"
git push origin main
bash release.sh                                # tag + GH release + tagged image build
```

## Anti-patterns to avoid

- **Skipping live test because unit tests pass.** Never. The proxy interacts
  with a stochastic model whose behaviour you cannot fully predict at the
  unit-test level. Every prior "this can't possibly regress" turned out to.
- **Reordering the injected examples on instinct.** Empirical evidence wins
  over intuition; verify with `voice-test.sh` before keeping a reorder.
- **Deploying without confirming the image SHA on the Pi.** A failed build
  silently leaves the previous image in place; deploying the old image looks
  like the new code "didn't work."
- **Adding the `[1.0.X]` section in a feature branch.** Versioning happens
  on `main` after the squash-merge (step 11), not in the feature branch.
  Otherwise the squash collapses the version bump and produces a confusing
  ancestry.
