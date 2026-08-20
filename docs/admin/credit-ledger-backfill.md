# Backfilling the credit ledger

The [credit ledger](../credit-ledger.md) only records movements made after it
was wired up. `backfill_credit_ledger` reconstructs what came before, from the
evidence that survives.

Run it once, per deployment, after upgrading to a release that records project
drawdown. It is not needed for a new installation.

```bash
waldur backfill_credit_ledger --dry-run
```

## What can and cannot be recovered

Read this before deciding whether to write anything. The command is precise
about which parts of history are evidence and which are inference:

| Movement | Recoverable? |
|----------|--------------|
| **Usage draws** | Yes. Each wrote a negative invoice item carrying the amount, the project and the billing month. One caveat: when a balance is exhausted mid-month the item is written net of tax while the balance gave up the full value, so the last draw of an exhausted credit is understated by its tax share. |
| **Minimal-consumption draws** | Amount yes, month no. They never wrote an invoice item; their only trace is an audit event, which carries no billing period. The month is inferred from when the event was emitted. |
| **The granted amount** | No. Every event that moves a balance in bulk records whole units only, so chaining them accumulates error. |

Whatever the evidence cannot explain is written as a single labelled row, so
that `granted = used + lost + remaining` holds by construction while the part
that is a plug rather than a record stays visible. Its sign says which of two
things it is: a positive remainder is value the credit already held, a negative
one is drawdown that left no trace at all — and the command warns about the
latter, because it can also mean drawdown is being attributed to the wrong
credit.

!!! warning "Coverage shrinks with age"
    Audit events are subject to retention-based cleanup, so how much history
    survives varies per deployment and decreases over time. `--dry-run` reports
    what evidence exists before anything is written.

## Inferred months

A minimal-consumption draw's month is inferred as the month before the audit
event was emitted. That holds for scheduled invoice finalization, and is wrong
for a manual re-run or a seeded history.

If your deployment has re-run finalization by hand, prefer:

```bash
waldur backfill_credit_ledger --infer-period=none
```

Those rows then count towards the totals but towards no month, which is
honest — a monthly breakdown that quietly attributes drawdown to the wrong
month is worse than one that admits it does not know.

## Options

| Option | Effect |
|--------|--------|
| `--dry-run` | Report what would be written; write nothing. |
| `--customer <uuid>` | Only that organization's credits, its projects included. |
| `--project <uuid>` | Only that project's allocation. The organization credit is a separate balance and is left alone. |
| `--since` / `--until` | Ignore evidence outside these billing months (`YYYY-MM`). |
| `--infer-period` | `previous-month` (default) or `none`; see above. |
| `--no-opening-balance` | Skip the balancing row. Totals then no longer reconcile to the current value of any credit granted before the ledger existed. |
| `--force` | Redo credits that already carry backfilled rows. |

Months the ledger already records are skipped, so backfilling is not
all-or-nothing: a credit older than the ledger has months on both sides of the
line, and only those without a row of their own are reconstructed.

## `--force` deletes rows

This is the one place anything is removed from an otherwise append-only table.
`--force` deletes the rows the command itself wrote for a credit and writes
them again; rows recorded by the live ledger are never touched.

Reconstructed rows are identified by their comment, which begins with
`backfill:`. Do not use that prefix when writing a staff adjustment comment, or
a later `--force` will delete the adjustment along with the reconstruction.
