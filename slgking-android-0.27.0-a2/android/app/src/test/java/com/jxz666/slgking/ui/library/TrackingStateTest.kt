package com.jxz666.slgking.ui.library

import org.junit.Assert.*
import org.junit.Test

class TrackingStateTest {
    @Test fun lateCallbackCannotReplaceLatestResult() {
        val gate = TrackingRequestGate()
        val first = gate.begin()
        val second = gate.begin()
        assertFalse(gate.owns(first))
        assertTrue(gate.owns(second))
        gate.invalidate()
        assertFalse(gate.owns(second))
    }
    @Test fun sectionsOwnTheirRequestsIndependently() {
        val updates = TrackingRequestGate()
        val collections = TrackingRequestGate()
        val updateRequest = updates.begin()
        val collectionRequest = collections.begin()
        collections.begin()
        assertTrue(updates.owns(updateRequest))
        assertFalse(collections.owns(collectionRequest))
    }
    @Test fun unknownCountsNeverPretendToBeZero() {
        assertEquals("…", trackingCountLabel(false, false, null, 0))
        assertEquals("…", trackingCountLabel(false, true, null, 0))
        assertEquals("—", trackingCountLabel(false, false, "offline", 0))
        assertEquals("0", trackingCountLabel(true, false, null, 0))
    }
    @Test fun knownCountsRemainVisibleDuringRefreshAndFailure() {
        assertEquals("1", trackingCountLabel(true, true, null, 1))
        assertEquals("91", trackingCountLabel(true, false, "offline", 91))
        assertEquals("0", trackingCountLabel(true, false, null, -1))
    }
    @Test fun repeatedMarkIsRejectedUntilActionCompletes() {
        val gate = TrackingMarkGate()
        assertTrue(gate.begin(7))
        assertFalse(gate.begin(7))
        assertTrue(gate.begin(8))
        assertEquals(setOf(7, 8), gate.snapshot())
        gate.finish(7)
        assertTrue(gate.begin(7))
    }
    @Test fun actionSnapshotCannotBeChangedByLaterActions() {
        val gate = TrackingMarkGate()
        gate.begin(1)
        val before = gate.snapshot()
        gate.finish(1)
        assertEquals(setOf(1), before)
        assertEquals(emptySet<Int>(), gate.snapshot())
    }
}
