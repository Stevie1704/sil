/* One object per fixture type, so an optimized build keeps every type in
 * DWARF. This is the translation unit a project writes for its own
 * interface header. */
#include "dwarf_layout.h"

AdasInput dwarf_layout_input;
struct AdasOutput dwarf_layout_output;

/* An unrelated typedef of void: the index must skip it. */
typedef void dwarf_layout_nothing;
dwarf_layout_nothing *dwarf_layout_opaque_handle;
