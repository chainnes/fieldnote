# Seed data

These files are the public **CC0 1.0** synthetic pack from
[DoreenSteven/CWB_SJ](https://github.com/DoreenSteven/CWB_SJ)
(`standard` pack). Everything in them is fictional. Names use example.org
addresses. The files are the public dataset for that problem statement.

Copied here so the desk runs without a network fetch and without private files.

| File | Used by Fieldnote |
| --- | --- |
| projects.csv | Yes |
| people.csv | Yes |
| tasks_master.csv | Yes, as the editable master plan |
| dependencies.csv | Yes |
| plan_snapshots.csv | Yes, read-only baseline and current snapshots |
| meeting_notes.jsonl | Yes |
| emails.csv | Yes |
| approvals_queue.csv | No. Rows point at projects, tasks, and people that are not in this pack, so loading them would invent work. |
| LICENSE | CC0 1.0 legal text |
| README_dataset_dictionary.json | Column dictionary from the pack |

The master task rows match the `baseline` snapshot. The `current` snapshot disagrees on many rows. Fieldnote shows that gap and does not silently copy the snapshot onto the plan.
