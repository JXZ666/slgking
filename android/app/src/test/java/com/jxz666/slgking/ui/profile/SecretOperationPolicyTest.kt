package com.jxz666.slgking.ui.profile

import org.junit.Assert.*
import org.junit.Test

class SecretOperationPolicyTest {
    @Test fun creationLocksBeforeRequestAndRejectsRepeatSubmission() {
        val initial = SecretOperationState<String>()
        val creating = initial.begin("account.create")!!
        assertTrue(creating.locked)
        assertEquals("account.create", creating.operation)
        assertNull(creating.begin("account.create"))
        assertNull(creating.begin("account.rotate"))
        assertTrue(creating.acknowledged().locked)
    }
    @Test fun pendingResultIsRetainedForRecreatedConsumer() {
        var activityState = SecretOperationState<String>().begin("account.create")!!
        activityState = activityState.succeeded("one-time-key-and-recovery")
        val firstUi = activityState.pending
        // Only the consumer is discarded; the Activity ViewModel owns the state.
        val recreatedUi = activityState.pending
        assertEquals(firstUi, recreatedUi)
        assertTrue(activityState.locked)
        assertNull(activityState.operation)
        assertNull(activityState.begin("account.create"))
    }
    @Test fun explicitAcknowledgementClearsCredentialsAndUnlocks() {
        val pending = SecretOperationState<String>().begin("account.create")!!.succeeded("keys")
        val confirmed = pending.acknowledged()
        assertNull(confirmed.pending)
        assertFalse(confirmed.locked)
        assertNotNull(confirmed.begin("account.rotate"))
    }
    @Test fun completionCleanupAndFailureCannotDiscardSuccessfulResponse() {
        val pending = SecretOperationState<String>().begin("account.rotate")!!.succeeded("new-keys")
        val cleaned = pending.failed()
        assertEquals("new-keys", cleaned.pending)
        assertTrue(cleaned.locked)
    }
    @Test fun FailedOperationUnlocksWithoutCreatingFakeCredentials() {
        val failed = SecretOperationState<String>().begin("account.create")!!.failed()
        assertFalse(failed.locked)
        assertNull(failed.pending)
        assertNotNull(failed.begin("account.create"))
    }
}
