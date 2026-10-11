# V0.7.2B.7 / C.6 — Tesla ↔ Robot Binding Hotfix

## Trigger

The validated 603809 discovery proposal showed:

```text
robot_context_signal_tags:
  MASS_PRODUCTION
  BATCH_SUPPLY

tesla_context_signal_tags:
  PURCHASE_ORDER
```

The official document explains why: one Q&A discusses humanoid/robot products and partial
batch supply, while a *different* Q&A discusses automotive differential customers including
Tesla and says automotive orders are sufficient.

These are both true document facts, but they are **not one Tesla-humanoid fact**.

## B.7 rule

A supplier/new-candidate Stage2 gate now requires:

```text
Tesla/Optimus context
+
robot/humanoid context
+
supplier-commercial milestone
```

bound to the **same semantic block / same bounded Q&A window**.

`tesla_context_signal_tags` is retained for audit only.

The authoritative gate field is:

```text
tesla_robot_context_signal_tags
tesla_robot_link_eligible
```

Therefore:

```text
robot batch supply in Q1
+ automotive Tesla orders in Q2
≠ Tesla/Optimus supplier evidence
```

but:

```text
"Tesla Optimus humanoid robot project entered batch supply"
→ Stage2 gate candidate (still human review)
```

## C.6

Discovery inference logic is unchanged. The queue now exposes both generic Tesla-business
tags and Tesla↔robot-bound tags so human review cannot mistake the former for an Optimus link.

No Registry, Canonicalization, Theme Stage, broker, or order mutation is introduced.
