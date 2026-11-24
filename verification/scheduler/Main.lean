import Scheduler

/-!
# `scheduler-model`: run the proven model on one input file

Input (decimal, one item per line): the duration, then one line per entry in
registration order, `period offset priority index`. Output: one line per
Activation, `time index`. Decimal text keeps every value through UINT64_MAX
exact.
-/

open Scheduler

def parseU64 (field : String) : Except String UInt64 :=
  match field.toNat? with
  | some n => if n < UInt64.size then pure (UInt64.ofNat n) else throw s!"out of range: {field}"
  | none => throw s!"not a natural number: {field}"

def parseEntry (line : String) : Except String Entry := do
  match line.splitOn " " with
  | [period, offset, priority, index] =>
    let some priority := priority.toInt? | throw s!"not an integer: {priority}"
    let some index := index.toNat? | throw s!"not a natural number: {index}"
    let period ← parseU64 period
    if period == 0 then throw "period must be positive"
    pure ⟨period, ← parseU64 offset, priority, index⟩
  | _ => throw s!"expected `period offset priority index`: {line}"

def parseInput (text : String) : Except String Input := do
  match (text.splitOn "\n").filter (· ≠ "") with
  | [] => throw "empty input"
  | duration :: entries =>
    let entries ← entries.mapM parseEntry
    let indices := entries.map (·.index)
    if indices.eraseDups.length ≠ indices.length then
      throw "registration indices must be distinct"
    pure ⟨← parseU64 duration, entries⟩

def main (args : List String) : IO UInt32 := do
  let [path] := args | do
    IO.eprintln "usage: scheduler-model INPUT"
    return 2
  match parseInput (← IO.FS.readFile path) with
  | .error message =>
    IO.eprintln s!"scheduler-model: {message}"
    return 2
  | .ok input =>
    let out ← IO.getStdout
    for a in model input do
      out.putStrLn s!"{a.time} {a.index}"
    return 0
