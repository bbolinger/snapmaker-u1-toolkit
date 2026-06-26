# Telegram STL ZIP ingest note

## Context

Telegram/Hermes document ingestion may reject raw `.stl` attachments with an unsupported-document-type message while accepting `.zip` attachments. For Snapmaker U1 workflows, this is not a reason to ask the operator to paste contents or abandon the staged slicer flow.

Tracking upstream fix: <https://github.com/NousResearch/hermes-agent/issues/53249>

## Durable workflow

1. If the user sends a `.zip` containing an `.stl`/`.3mf`, inspect the archive and extract the model to a stable local path.
2. Immediately continue with the required first workflow call:
   `python3 /opt/data/scripts/u1_slice_workflow.py <extracted-model> --json-events`
3. Preserve the normal staged question flow after that. The ZIP is just an ingress workaround; it does not change any printer safety gate.

## Pitfall

If the operator adds extra preferences in a prompt answer that are not represented by the workflow's current options or `next_command` fields (for example, answering `No supports, no brim` when only supports are being requested), do not invent flags or hand-edit the command. Match the represented option, run its `next_command` verbatim, and only describe settings that the workflow actually emitted or confirmed.
