import Scheduler.Spec
import Scheduler.Time

/-!
# The executable scheduler model

The successful periodic path of `Engine::run` (`kernel/src/engine.cpp`): choose
the earliest due Slot, order its due entries by priority and then registration
index, and advance each with checked `uint64_t` addition. An entry whose next
time is unrepresentable, or at or beyond the duration, retires.
-/

namespace Scheduler

/-- An entry and its next Activation; `none` once it has retired. -/
structure Task where
  entry : Entry
  next : Option UInt64

def Task.start (duration : UInt64) (e : Entry) : Task :=
  ⟨e, if e.offset < duration then some e.offset else none⟩

/-- The earliest next Activation of the entries that have not retired. -/
def earliest : List Task → Option UInt64
  | [] => none
  | t :: ts =>
    match t.next, earliest ts with
    | none, r => r
    | some n, none => some n
    | some n, some m => some (if n ≤ m then n else m)

/-- The order of the due entries in one Slot. -/
def slotOrder (a b : Task) : Bool :=
  a.entry.priority < b.entry.priority ||
    (a.entry.priority == b.entry.priority && a.entry.index ≤ b.entry.index)

/-- The next Activation after `slot`, or `none` when the entry retires.

`Option.filter`, not a `match`: on Lean 4.36, generating the equation lemma
of a `match` on a `checkedAdd` application does not finish. In proofs,
`cases h : checkedAdd a b` works. -/
def nextAfter (duration slot period : UInt64) : Option UInt64 :=
  (checkedAdd slot period).filter (· < duration)

def Task.advance (duration slot : UInt64) (t : Task) : Task :=
  if t.next = some slot then { t with next := nextAfter duration slot t.entry.period }
  else t

/-- The Activations of one Slot, in execution order. -/
def slotActivations (slot : UInt64) (ts : List Task) : List Act :=
  ((ts.filter (·.next == some slot)).mergeSort slotOrder).map
    fun t => ⟨slot.toNat, t.entry.priority, t.entry.index⟩

/-- Run one Slot: its Activations and the state after it. -/
def step (duration : UInt64) (ts : List Task) : Option (List Act × List Task) :=
  (earliest ts).map fun slot =>
    (slotActivations slot ts, ts.map (Task.advance duration slot))

def Task.weight (t : Task) : Nat :=
  match t.next with
  | some n => 2 ^ 64 - n.toNat
  | none => 0

/-- Decreases on every Slot, so it bounds the number of Slots. -/
def measure : List Task → Nat
  | [] => 0
  | t :: ts => t.weight + measure ts

def runFor (duration : UInt64) : Nat → List Task → List Act
  | 0, _ => []
  | fuel + 1, ts =>
    match step duration ts with
    | none => []
    | some (out, ts') => out ++ runFor duration fuel ts'

def Input.start (inp : Input) : List Task := inp.entries.map (Task.start inp.duration)

/-- The model's complete ordered Activation trace. -/
def model (inp : Input) : List Act :=
  runFor inp.duration (measure inp.start) inp.start

end Scheduler
