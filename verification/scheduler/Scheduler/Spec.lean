/-!
# The declarative scheduling contract

A Run has a finite list of periodic entries: Native Tasks and Process Steps
(offset zero). Each entry has a positive period, an offset, a signed priority
and a registration index. All times are `uint64_t` values; the contract is
stated over mathematical natural numbers (`UInt64.toNat`), so no sum wraps.

- An entry is due exactly once at every `offset + k * period` (`k ≥ 0`) in
  `[0, duration)`.
- The trace is ordered by Virtual time, then priority, then registration index.
-/

namespace Scheduler

/-- One Activation: the Virtual time, and the entry's priority and index. -/
structure Act where
  time : Nat
  priority : Int
  index : Nat
  deriving DecidableEq, Repr

/-- The total order of the trace: time, then priority, then index. -/
def Act.lt (a b : Act) : Prop :=
  a.time < b.time ∨
    (a.time = b.time ∧
      (a.priority < b.priority ∨ (a.priority = b.priority ∧ a.index < b.index)))

instance (a b : Act) : Decidable (a.lt b) := by unfold Act.lt; infer_instance

theorem Act.lt_irrefl (a : Act) : ¬ a.lt a := by
  unfold Act.lt; omega

theorem Act.lt_asymm {a b : Act} (h : a.lt b) : ¬ b.lt a := by
  unfold Act.lt at *; omega

structure Entry where
  period : UInt64
  offset : UInt64
  priority : Int
  index : Nat
  deriving Repr

structure Input where
  duration : UInt64
  entries : List Entry
  deriving Repr

/-- The inputs the theorem covers: positive periods, distinct indices. -/
structure Input.Valid (inp : Input) : Prop where
  period_pos : ∀ e ∈ inp.entries, 0 < e.period.toNat
  index_nodup : (inp.entries.map (·.index)).Nodup

/-- The `k`-th Activation of an entry, over mathematical integers. -/
def Entry.activation (e : Entry) (k : Nat) : Act :=
  ⟨e.offset.toNat + k * e.period.toNat, e.priority, e.index⟩

/-- `a` is due: it is the `k`-th Activation of an entry, before the duration. -/
def Due (inp : Input) (a : Act) : Prop :=
  ∃ e ∈ inp.entries, ∃ k, a = e.activation k ∧ a.time < inp.duration.toNat

/-- The declarative schedule: strictly ordered, and holding exactly the due
Activations. -/
def IsSchedule (inp : Input) (trace : List Act) : Prop :=
  trace.Pairwise Act.lt ∧ ∀ a, a ∈ trace ↔ Due inp a

/-- Two strictly ordered lists with the same members are equal. -/
theorem eq_of_pairwise_lt :
    ∀ {l₁ l₂ : List Act}, l₁.Pairwise Act.lt → l₂.Pairwise Act.lt →
      (∀ a, a ∈ l₁ ↔ a ∈ l₂) → l₁ = l₂
  | [], [], _, _, _ => rfl
  | [], b :: _, _, _, hm => by simp at hm; exact absurd (hm b) (by simp)
  | a :: _, [], _, _, hm => by simp at hm; exact absurd (hm a) (by simp)
  | a :: t, b :: u, h₁, h₂, hm => by
    rw [List.pairwise_cons] at h₁ h₂
    have hab : a = b := by
      have ha := (hm a).1 (by simp)
      have hb := (hm b).2 (by simp)
      simp only [List.mem_cons] at ha hb
      rcases ha with ha | ha
      · exact ha
      rcases hb with hb | hb
      · exact hb.symm
      exact absurd (h₁.1 b hb) (Act.lt_asymm (h₂.1 a ha))
    subst hab
    have htu : t = u := by
      refine eq_of_pairwise_lt h₁.2 h₂.2 fun x => ⟨fun hx => ?_, fun hx => ?_⟩
      · have := (hm x).1 (by simp [hx])
        simp only [List.mem_cons] at this
        rcases this with rfl | h
        · exact absurd (h₁.1 _ hx) (Act.lt_irrefl _)
        · exact h
      · have := (hm x).2 (by simp [hx])
        simp only [List.mem_cons] at this
        rcases this with rfl | h
        · exact absurd (h₂.1 _ hx) (Act.lt_irrefl _)
        · exact h
    rw [htu]

theorem nodup_of_pairwise_lt {l : List Act} (h : l.Pairwise Act.lt) :
    l.Nodup :=
  h.imp fun {x _} hxy heq => by subst heq; exact Act.lt_irrefl x hxy

/-- The declarative schedule has at most one solution. -/
theorem IsSchedule.unique {inp : Input} {t₁ t₂ : List Act}
    (h₁ : IsSchedule inp t₁) (h₂ : IsSchedule inp t₂) : t₁ = t₂ :=
  eq_of_pairwise_lt h₁.1 h₂.1 fun a => (h₁.2 a).trans (h₂.2 a).symm

/-- In a schedule, every due Activation occurs exactly once. -/
theorem IsSchedule.count_due {inp : Input} {trace : List Act}
    (h : IsSchedule inp trace) {a : Act} (ha : Due inp a) :
    trace.count a = 1 :=
  List.Nodup.count_of_mem (nodup_of_pairwise_lt h.1) ((h.2 a).2 ha)

end Scheduler
