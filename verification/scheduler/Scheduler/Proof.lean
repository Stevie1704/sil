import Scheduler.Model

/-!
# The model implements the declarative schedule

`model_isSchedule` and `model_eq_schedule` hold for every valid input: the
proof does not depend on the number of entries or on the magnitude of times.
-/

namespace Scheduler

/-! ## The earliest Slot -/

theorem earliest_eq_none {ts : List Task} (h : earliest ts = none) :
    ∀ t ∈ ts, t.next = none := by
  induction ts with
  | nil => simp
  | cons t ts ih =>
    unfold earliest at h
    split at h <;> simp_all

theorem earliest_eq_some {ts : List Task} {s : UInt64} (h : earliest ts = some s) :
    (∃ t ∈ ts, t.next = some s) ∧
      ∀ t ∈ ts, ∀ n, t.next = some n → s.toNat ≤ n.toNat := by
  induction ts generalizing s with
  | nil => simp [earliest] at h
  | cons t ts ih =>
    unfold earliest at h
    split at h
    · rename_i hn _
      obtain ⟨⟨u, hu, hus⟩, hle⟩ := ih h
      refine ⟨⟨u, by simp [hu], hus⟩, ?_⟩
      intro v hv n hvn
      simp only [List.mem_cons] at hv
      rcases hv with rfl | hv
      · simp_all
      · exact hle v hv n hvn
    · rename_i n hn hr
      cases h
      refine ⟨⟨t, by simp, hn⟩, ?_⟩
      intro v hv m hvm
      simp only [List.mem_cons] at hv
      rcases hv with rfl | hv
      · simp_all
      · exact absurd hvm (by simp [earliest_eq_none hr v hv])
    · rename_i n m hn hr
      obtain ⟨⟨u, hu, hum⟩, hle⟩ := ih hr
      cases h
      split
      · rename_i hnm
        rw [UInt64.le_iff_toNat_le] at hnm
        refine ⟨⟨t, by simp, hn⟩, ?_⟩
        intro v hv k hvk
        simp only [List.mem_cons] at hv
        rcases hv with rfl | hv
        · simp_all
        · have := hle v hv k hvk; omega
      · rename_i hnm
        rw [UInt64.le_iff_toNat_le] at hnm
        refine ⟨⟨u, by simp [hu], hum⟩, ?_⟩
        intro v hv k hvk
        simp only [List.mem_cons] at hv
        rcases hv with rfl | hv
        · simp_all; omega
        · exact hle v hv k hvk

/-! ## One entry across one Slot -/

/-- The Activations still ahead of one entry. -/
def Task.Pending (duration : UInt64) (t : Task) (a : Act) : Prop :=
  ∃ n, t.next = some n ∧ ∃ k,
    a = ⟨n.toNat + k * t.entry.period.toNat, t.entry.priority, t.entry.index⟩ ∧
      a.time < duration.toNat

/-- The state invariant of one entry: its next time is before the duration. -/
def Task.Ok (duration : UInt64) (t : Task) : Prop :=
  0 < t.entry.period.toNat ∧ ∀ n, t.next = some n → n.toNat < duration.toNat

theorem nextAfter_eq_some {d s p m : UInt64} :
    nextAfter d s p = some m ↔ checkedAdd s p = some m ∧ m < d := by
  unfold nextAfter
  cases h : checkedAdd s p with
  | none => simp
  | some n => simp

/-- A next time before the duration is representable, so the entry stays. -/
theorem nextAfter_of_lt {d s p : UInt64} (h : s.toNat + p.toNat < d.toNat) :
    ∃ m, nextAfter d s p = some m ∧ m.toNat = s.toNat + p.toNat := by
  have := UInt64.toNat_lt d
  cases hc : checkedAdd s p with
  | none => exact absurd (checkedAdd_eq_none.1 hc) (by omega)
  | some m =>
    have hm := (checkedAdd_eq_some.1 hc).2
    exact ⟨m, nextAfter_eq_some.2 ⟨hc, UInt64.lt_iff_toNat_lt.2 (by omega)⟩, hm⟩

@[simp] theorem Task.advance_entry (d s : UInt64) (t : Task) :
    (t.advance d s).entry = t.entry := by
  unfold Task.advance; split <;> rfl

theorem Task.advance_next_due {d s : UInt64} {t : Task} (hs : t.next = some s) :
    (t.advance d s).next = nextAfter d s t.entry.period := by
  unfold Task.advance; simp [hs]

theorem Task.advance_not_due {d s : UInt64} {t : Task} (hs : t.next ≠ some s) :
    t.advance d s = t := by
  unfold Task.advance; simp [hs]

theorem Task.advance_ok {d s : UInt64} {t : Task} (h : t.Ok d) : (t.advance d s).Ok d := by
  refine ⟨by simpa using h.1, fun n hn => ?_⟩
  by_cases hs : t.next = some s
  · rw [Task.advance_next_due hs] at hn
    exact UInt64.lt_iff_toNat_lt.1 (nextAfter_eq_some.1 hn).2
  · rw [Task.advance_not_due hs] at hn; exact h.2 n hn

/-- An unrepresentable next time retires the entry. -/
theorem Task.advance_unrepresentable {d s : UInt64} {t : Task} (hs : t.next = some s)
    (h : 2 ^ 64 ≤ s.toNat + t.entry.period.toNat) : (t.advance d s).next = none := by
  rw [Task.advance_next_due hs]
  unfold nextAfter
  rw [checkedAdd_eq_none.2 h]
  rfl

/-- Across Slot `s`, an entry's pending Activations split into the one it runs
in `s` and the ones still pending after it. -/
theorem Task.pending_advance {d s : UInt64} {t : Task} (hok : t.Ok d) (a : Act) :
    t.Pending d a ↔
      (t.next = some s ∧ a = ⟨s.toNat, t.entry.priority, t.entry.index⟩) ∨
        (t.advance d s).Pending d a := by
  by_cases hs : t.next = some s
  · have hsd := hok.2 s hs
    have hadv := Task.advance_next_due (d := d) hs
    constructor
    · rintro ⟨n, hn, k, rfl, hk⟩
      rw [hs] at hn; cases hn
      cases k with
      | zero => left; simp [hs]
      | succ j =>
        right
        simp only [Nat.succ_mul] at hk
        have := Nat.zero_le (j * t.entry.period.toNat)
        obtain ⟨m, hm, hmv⟩ := nextAfter_of_lt (d := d) (s := s)
          (p := t.entry.period) (by omega)
        refine ⟨m, by rw [hadv, hm], j, ?_, ?_⟩
        · simp only [Task.advance_entry, Nat.add_mul, Nat.one_mul, hmv, Act.mk.injEq, and_true]
          omega
        · simp only [Nat.add_mul, Nat.one_mul]; omega
    · rintro (⟨_, rfl⟩ | ⟨m, hm, j, rfl, hj⟩)
      · exact ⟨s, hs, 0, by simp, hsd⟩
      · rw [hadv] at hm
        have hmv := (checkedAdd_eq_some.1 (nextAfter_eq_some.1 hm).1).2
        simp only [Task.advance_entry] at hj ⊢
        refine ⟨s, hs, j + 1, ?_, hj⟩
        simp only [Nat.add_mul, Nat.one_mul, hmv, Act.mk.injEq, and_true]; omega
  · rw [Task.advance_not_due hs]; simp [hs]

/-- After Slot `s`, every pending Activation of an entry is later than `s`. -/
theorem Task.pending_after {d s : UInt64} {t : Task} (hok : t.Ok d)
    (hle : ∀ n, t.next = some n → s.toNat ≤ n.toNat) {a : Act}
    (h : (t.advance d s).Pending d a) : s.toNat < a.time := by
  obtain ⟨m, hm, k, rfl, _⟩ := h
  simp only [Task.advance_entry]
  have := Nat.zero_le (k * t.entry.period.toNat)
  by_cases hs : t.next = some s
  · rw [Task.advance_next_due hs] at hm
    have hmv := (checkedAdd_eq_some.1 (nextAfter_eq_some.1 hm).1).2
    have := hok.1
    omega
  · rw [Task.advance_not_due hs] at hm
    have h1 := hle m hm
    have : m.toNat ≠ s.toNat := fun h => hs (by rw [hm, UInt64.toNat_inj.1 h])
    omega

/-! ## The whole state across one Slot -/

def Pending (d : UInt64) (ts : List Task) (a : Act) : Prop := ∃ t ∈ ts, t.Pending d a

structure Inv (d : UInt64) (ts : List Task) : Prop where
  ok : ∀ t ∈ ts, t.Ok d
  nodup : (ts.map (·.entry.index)).Nodup

theorem Inv.step {d s : UInt64} {ts : List Task} (h : Inv d ts) :
    Inv d (ts.map (Task.advance d s)) where
  ok := by
    intro t ht
    obtain ⟨u, hu, rfl⟩ := List.mem_map.1 ht
    exact Task.advance_ok (h.ok u hu)
  nodup := by
    rw [List.map_map]
    have : ((·.entry.index) ∘ Task.advance d s) = (·.entry.index) := by
      funext t; simp
    rw [this]; exact h.nodup

theorem mem_slotActivations {s : UInt64} {ts : List Task} {a : Act} :
    a ∈ slotActivations s ts ↔
      ∃ t ∈ ts, t.next = some s ∧ a = ⟨s.toNat, t.entry.priority, t.entry.index⟩ := by
  unfold slotActivations
  simp only [List.mem_map, List.mem_mergeSort, List.mem_filter, beq_iff_eq]
  constructor
  · rintro ⟨t, ⟨ht, hs⟩, rfl⟩; exact ⟨t, ht, hs, rfl⟩
  · rintro ⟨t, ht, hs, rfl⟩; exact ⟨t, ⟨ht, hs⟩, rfl⟩

theorem slotOrder_trans (a b c : Task) :
    slotOrder a b = true → slotOrder b c = true → slotOrder a c = true := by
  simp only [slotOrder, Bool.or_eq_true, Bool.and_eq_true, decide_eq_true_eq, beq_iff_eq]
  omega

theorem slotOrder_total (a b : Task) : (slotOrder a b || slotOrder b a) = true := by
  simp only [slotOrder, Bool.or_eq_true, Bool.and_eq_true, decide_eq_true_eq, beq_iff_eq]
  omega

/-- The Activations of one Slot are in strict trace order. -/
theorem slotActivations_pairwise {d s : UInt64} {ts : List Task} (h : Inv d ts) :
    (slotActivations s ts).Pairwise Act.lt := by
  unfold slotActivations
  let due := ts.filter (·.next == some s)
  have hsorted := List.pairwise_mergeSort slotOrder_trans slotOrder_total due
  have hnodup : ((due.mergeSort slotOrder).map (·.entry.index)).Nodup := by
    have hsub : (due.map (·.entry.index)).Nodup :=
      h.nodup.sublist ((List.filter_sublist).map _)
    exact ((List.mergeSort_perm due slotOrder).map _).nodup_iff.2 hsub
  rw [List.pairwise_map]
  refine (hsorted.and (List.pairwise_map.1 hnodup)).imp ?_
  rintro t u ⟨hord, (hne : t.entry.index ≠ u.entry.index)⟩
  simp only [slotOrder, Bool.or_eq_true, Bool.and_eq_true, decide_eq_true_eq,
    beq_iff_eq] at hord
  unfold Act.lt
  simp only [true_and]
  omega

theorem pending_step {d s : UInt64} {ts : List Task} (h : Inv d ts) (a : Act) :
    Pending d ts a ↔
      a ∈ slotActivations s ts ∨ Pending d (ts.map (Task.advance d s)) a := by
  rw [mem_slotActivations]
  unfold Pending
  constructor
  · rintro ⟨t, ht, hp⟩
    rcases (Task.pending_advance (s := s) (h.ok t ht) a).1 hp with ⟨hs, rfl⟩ | hp'
    · exact Or.inl ⟨t, ht, hs, rfl⟩
    · exact Or.inr ⟨_, List.mem_map_of_mem ht, hp'⟩
  · rintro (⟨t, ht, hs, rfl⟩ | ⟨u, hu, hp⟩)
    · exact ⟨t, ht, (Task.pending_advance (h.ok t ht) _).2 (Or.inl ⟨hs, rfl⟩)⟩
    · obtain ⟨t, ht, rfl⟩ := List.mem_map.1 hu
      exact ⟨t, ht, (Task.pending_advance (h.ok t ht) a).2 (Or.inr hp)⟩

theorem pending_after_step {d s : UInt64} {ts : List Task} (h : Inv d ts)
    (he : earliest ts = some s) {a : Act}
    (hp : Pending d (ts.map (Task.advance d s)) a) : s.toNat < a.time := by
  obtain ⟨u, hu, hp⟩ := hp
  obtain ⟨t, ht, rfl⟩ := List.mem_map.1 hu
  exact Task.pending_after (h.ok t ht) ((earliest_eq_some he).2 t ht) hp

theorem not_pending_of_retired {d : UInt64} {ts : List Task}
    (h : ∀ t ∈ ts, t.next = none) (a : Act) : ¬ Pending d ts a := by
  rintro ⟨t, ht, n, hn, _⟩
  rw [h t ht] at hn; cases hn

/-! ## The measure bounds the number of Slots -/

theorem Task.weight_advance {d s : UInt64} {t : Task} (h : t.Ok d) :
    (t.advance d s).weight ≤ t.weight ∧
      (t.next = some s → (t.advance d s).weight < t.weight) := by
  by_cases hs : t.next = some s
  · have hlt := UInt64.toNat_lt s
    have hw : t.weight = 2 ^ 64 - s.toNat := by unfold Task.weight; rw [hs]
    have hlt' : (t.advance d s).weight < t.weight := by
      rw [hw]
      simp only [Task.weight, Task.advance_next_due hs]
      cases hn : nextAfter d s t.entry.period with
      | none => simp only; omega
      | some m =>
        simp only
        have hmv := (checkedAdd_eq_some.1 (nextAfter_eq_some.1 hn).1).2
        have := h.1
        omega
    exact ⟨Nat.le_of_lt hlt', fun _ => hlt'⟩
  · rw [Task.advance_not_due hs]; exact ⟨Nat.le_refl _, fun h => absurd h hs⟩

theorem measure_map_le {f : Task → Task} :
    ∀ {ts : List Task}, (∀ t ∈ ts, (f t).weight ≤ t.weight) →
      measure (ts.map f) ≤ measure ts
  | [], _ => Nat.le_refl _
  | t :: ts, h => by
    simp only [List.map_cons, measure]
    have := h t (by simp)
    have := measure_map_le (ts := ts) fun u hu => h u (by simp [hu])
    omega

theorem measure_step_lt {d s : UInt64} :
    ∀ {ts : List Task}, (∀ t ∈ ts, t.Ok d) → (∃ t ∈ ts, t.next = some s) →
      measure (ts.map (Task.advance d s)) < measure ts
  | [], _, hdue => by simp at hdue
  | t :: ts, hok, hdue => by
    have hts : ∀ u ∈ ts, u.Ok d := fun u hu => hok u (by simp [hu])
    have ht := Task.weight_advance (s := s) (hok t (by simp))
    simp only [List.map_cons, measure]
    obtain ⟨u, hu, hus⟩ := hdue
    simp only [List.mem_cons] at hu
    rcases hu with rfl | hu
    · have := ht.2 hus
      have := measure_map_le (f := Task.advance d s) (ts := ts)
        fun v hv => (Task.weight_advance (hts v hv)).1
      omega
    · have := measure_step_lt hts ⟨u, hu, hus⟩; have := ht.1; omega

theorem retired_of_measure_zero :
    ∀ {ts : List Task}, measure ts = 0 → ∀ t ∈ ts, t.next = none
  | [], _ => by simp
  | t :: ts, h => by
    simp only [measure] at h
    intro u hu
    simp only [List.mem_cons] at hu
    rcases hu with rfl | hu
    · unfold Task.weight at h
      cases hn : u.next with
      | none => rfl
      | some n => rw [hn] at h; have := UInt64.toNat_lt n; simp only at h; omega
    · exact retired_of_measure_zero (by omega) u hu

/-! ## The complete trace -/

theorem runFor_spec {d : UInt64} :
    ∀ (fuel : Nat) (ts : List Task), Inv d ts → measure ts ≤ fuel →
      (runFor d fuel ts).Pairwise Act.lt ∧
        ∀ a, a ∈ runFor d fuel ts ↔ Pending d ts a
  | 0, ts, _, hm => by
    have hr := retired_of_measure_zero (Nat.le_zero.1 hm)
    exact ⟨List.Pairwise.nil, fun a => by
      simp only [runFor, List.not_mem_nil, false_iff]
      exact not_pending_of_retired hr a⟩
  | fuel + 1, ts, h, hm => by
    unfold runFor step
    cases he : earliest ts with
    | none =>
      exact ⟨List.Pairwise.nil, fun a => by
        simp only [Option.map_none, List.not_mem_nil, false_iff]
        exact not_pending_of_retired (earliest_eq_none he) a⟩
    | some s =>
      simp only [Option.map_some]
      have hlt := measure_step_lt (s := s) h.ok (earliest_eq_some he).1
      obtain ⟨hpw, hmem⟩ := runFor_spec fuel _ (h.step (s := s)) (by omega)
      refine ⟨List.pairwise_append.2 ⟨slotActivations_pairwise h, hpw, ?_⟩, ?_⟩
      · intro a ha b hb
        obtain ⟨t, _, _, rfl⟩ := mem_slotActivations.1 ha
        exact Or.inl (pending_after_step h he ((hmem b).1 hb))
      · intro a
        rw [List.mem_append, hmem, pending_step h]

theorem Input.start_inv {inp : Input} (h : inp.Valid) : Inv inp.duration inp.start where
  ok := by
    intro t ht
    obtain ⟨e, he, rfl⟩ := List.mem_map.1 ht
    refine ⟨h.period_pos e he, fun n hn => ?_⟩
    unfold Task.start at hn
    simp only at hn
    split at hn
    · cases hn; rename_i hlt; exact UInt64.lt_iff_toNat_lt.1 hlt
    · cases hn
  nodup := by
    unfold Input.start
    rw [List.map_map]
    exact h.index_nodup

theorem Input.pending_start (inp : Input) (a : Act) :
    Pending inp.duration inp.start a ↔ Due inp a := by
  unfold Pending Input.start Due
  simp only [List.mem_map]
  constructor
  · rintro ⟨_, ⟨e, he, rfl⟩, n, hn, k, rfl, hk⟩
    unfold Task.start at hn
    simp only at hn
    split at hn
    · cases hn; exact ⟨e, he, k, rfl, hk⟩
    · cases hn
  · rintro ⟨e, he, k, rfl, hk⟩
    have hoff : e.offset < inp.duration := by
      rw [UInt64.lt_iff_toNat_lt]; unfold Entry.activation at hk; simp only at hk; omega
    exact ⟨_, ⟨e, he, rfl⟩, e.offset, by simp [Task.start, hoff], k, rfl, hk⟩

/-- **Trace equivalence.** For every valid input, the model's complete ordered
trace satisfies the declarative schedule. -/
theorem model_isSchedule {inp : Input} (h : inp.Valid) : IsSchedule inp (model inp) := by
  obtain ⟨hpw, hmem⟩ :=
    runFor_spec (measure inp.start) inp.start (Input.start_inv h) (Nat.le_refl _)
  exact ⟨hpw, fun a => (hmem a).trans (Input.pending_start inp a)⟩

/-- The model's trace is the declarative schedule: any list that satisfies the
schedule equals it. -/
theorem model_eq_schedule {inp : Input} (h : inp.Valid) {trace : List Act}
    (hs : IsSchedule inp trace) : model inp = trace :=
  (model_isSchedule h).unique hs

end Scheduler
