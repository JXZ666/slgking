package com.jxz666.slgking.ui.tools

import org.junit.Assert.*
import org.junit.Test
class ToolPoliciesTest {
    @Test fun unknownTotalsHaveNoDeterminateProgress() {
        assertNull(SyncStagePolicy.progress(100, null))
        assertNull(SyncStagePolicy.progress(100, 0))
        assertTrue(SyncStagePolicy.label("dlsite", "download", 2048, null, "bytes").contains("总量未知"))
    }
    @Test fun stageCountsClampAndDoNotOverflowInt() {
        assertEquals(100L to 100L, SyncStagePolicy.progress(120, 100))
        assertEquals(0L to 100L, SyncStagePolicy.progress(-1, 100))
        assertEquals(4_000_000_000L to 8_000_000_000L, SyncStagePolicy.progress(4_000_000_000, 8_000_000_000))
        val label = SyncStagePolicy.label("western_slg", "merge", 15, 30, "rows")
        assertTrue(label.contains("合并目录")); assertTrue(label.contains("15条 / 30条"))
    }
    @Test fun helpMatchesBodyAndTitleLocally() {
        assertTrue(searchHelp("头像").any { it.title.contains("个人") })
        assertTrue(searchHelp("JSON").any { it.title.contains("备份") || it.title.contains("个人") })
        assertEquals(androidHelp.size, searchHelp("  ").size)
        assertTrue(searchHelp("zzzznonexistent").isEmpty())
    }
}
