package com.jxz666.slgking.ui.common

import org.junit.Assert.*
import org.junit.Test

class NavigationScrollTest {
    @Test fun ActiveTabAndRotationDoNotResetConsumedEpoch() {
        assertFalse(shouldResetScroll(0, 0))
        assertFalse(shouldResetScroll(3, 3))
    }
    @Test fun SwitchingAwayAndBackRequiresOneReset() {
        assertTrue(shouldResetScroll(4, 3))
        assertFalse(shouldResetScroll(4, 4))
    }
}
