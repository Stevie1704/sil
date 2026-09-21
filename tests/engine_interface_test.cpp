#include <type_traits>

#include "engine.hpp"

static_assert(!std::is_constructible_v<sil::Engine, const sil::Manifest &,
                                       sil::RecordingSink *>);
static_assert(std::is_constructible_v<sil::Engine, sil::PreparedRun &&,
                                      sil::RecordingSink *>);
static_assert(!std::is_constructible_v<sil::Engine, sil::PreparedRun &,
                                       sil::RecordingSink *>);
static_assert(!std::is_default_constructible_v<sil::PreparedRun>);

int main() {}
