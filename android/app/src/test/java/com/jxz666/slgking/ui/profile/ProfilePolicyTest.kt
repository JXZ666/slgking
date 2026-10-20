package com.jxz666.slgking.ui.profile

import org.junit.Assert.*
import org.junit.Test

class ProfilePolicyTest {
    @Test fun accountSwitchRejectsBothSuccessAndFailureFromOldAccount() {
        val gate = ProfileFence()
        gate.select("A:1")
        val ticket = gate.request()
        gate.select("B:2")
        assertFalse(gate.accepts(ticket))
        assertTrue(gate.accepts(gate.request()))
    }
    @Test fun logoutAndReloginSameAccountRejectOldSession() {
        val gate = ProfileFence()
        gate.select("A:1")
        val old = gate.request()
        gate.select("guest:2")
        gate.select("A:3")
        assertFalse(gate.accepts(old))
    }
    @Test fun localStatusReadsDoNotInvalidateSameIdentityOrTriggerRequests() {
        val gate = ProfileFence()
        gate.select("A:1")
        val ticket = gate.request()
        repeat(10) { assertFalse(gate.select("A:1")) }
        assertTrue(gate.accepts(ticket))
    }
    @Test fun newerRequestRejectsOlderLateResult() {
        val gate = ProfileFence()
        gate.select("A:1")
        val old = gate.request()
        val latest = gate.request()
        assertFalse(gate.accepts(old))
        assertTrue(gate.accepts(latest))
    }
    @Test fun avatarNamesArePrivateStableAndIdentitySeparated() {
        val first = AvatarPolicy.filename("A/../../secret")
        assertEquals(first, AvatarPolicy.filename("A/../../secret"))
        assertNotEquals(first, AvatarPolicy.filename("B"))
        assertTrue(first.matches(Regex("[0-9a-f]{64}\\.jpg")))
        assertFalse(first.contains("secret"))
    }
    @Test fun avatarLimitsAndDefaultInitial() {
        assertEquals(256, AvatarPolicy.SIDE)
        assertEquals(65536, AvatarPolicy.MAX_BYTES)
        assertEquals("菊", AvatarPolicy.initial(" 菊千代 "))
        assertEquals("玩", AvatarPolicy.initial("  "))
    }
}
