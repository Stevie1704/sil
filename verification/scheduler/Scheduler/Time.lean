/-!
# Checked Virtual time addition

The model of `sil::virtual_time_after` (`kernel/src/virtual_time.hpp`): the
`uint64_t` sum, or nothing when the sum cannot be represented.
-/

namespace Scheduler

def checkedAdd (from_ns delta_ns : UInt64) : Option UInt64 :=
  if delta_ns > 0xFFFFFFFFFFFFFFFF - from_ns then none else some (from_ns + delta_ns)

/-- The guard of `checkedAdd` holds exactly when the sum is unrepresentable. -/
theorem checkedAdd_guard (a b : UInt64) :
    b > 0xFFFFFFFFFFFFFFFF - a ↔ 2 ^ 64 ≤ a.toNat + b.toNat := by
  have ha := UInt64.toNat_lt a
  have hmax : (0xFFFFFFFFFFFFFFFF - a).toNat = 2 ^ 64 - 1 - a.toNat := by
    rw [UInt64.toNat_sub_of_le _ _ (UInt64.le_iff_toNat_le.2 (by simp; omega))]
    simp
  rw [gt_iff_lt, UInt64.lt_iff_toNat_lt, hmax]
  omega

/-- A representable sum is the exact mathematical sum. -/
theorem checkedAdd_eq_some {a b c : UInt64} :
    checkedAdd a b = some c ↔
      a.toNat + b.toNat < 2 ^ 64 ∧ c.toNat = a.toNat + b.toNat := by
  unfold checkedAdd
  split
  · rename_i h; rw [checkedAdd_guard] at h; simp; omega
  · rename_i h
    rw [checkedAdd_guard] at h
    constructor
    · intro hc; cases hc; rw [UInt64.toNat_add]; omega
    · intro ⟨_, hc⟩
      congr
      apply UInt64.toNat_inj.1
      rw [UInt64.toNat_add]; omega

/-- An unrepresentable sum gives nothing: the addition never wraps around. -/
theorem checkedAdd_eq_none {a b : UInt64} :
    checkedAdd a b = none ↔ 2 ^ 64 ≤ a.toNat + b.toNat := by
  unfold checkedAdd
  split
  · rename_i h; rw [checkedAdd_guard] at h; simp [h]
  · rename_i h; rw [checkedAdd_guard] at h; simp; omega

end Scheduler
