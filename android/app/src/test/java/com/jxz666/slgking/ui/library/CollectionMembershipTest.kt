package com.jxz666.slgking.ui.library

import org.junit.Assert.*
import org.junit.Test

class CollectionMembershipTest {
    @Test fun removingOneDoesNotClearOtherCollections() {
        assertEquals(listOf(2, 9), remainingCollectionIds(listOf(2, 5, 9), 5))
    }
    @Test fun gameWithOnlyOneMembershipLeavesWithoutDeletingGame() {
        assertTrue(remainingCollectionIds(listOf(5), 5).isEmpty())
    }
    @Test fun repeatedRemovalIsIdempotent() {
        val first = remainingCollectionIds(listOf(2, 5, 9), 5)
        assertEquals(first, remainingCollectionIds(first, 5))
    }
    @Test fun absentMembershipNeverAddsOrDropsOtherCollections() {
        assertEquals(listOf(2, 9), remainingCollectionIds(listOf(2, 9), 5))
        assertTrue(remainingCollectionIds(emptyList(), 5).isEmpty())
    }
}
