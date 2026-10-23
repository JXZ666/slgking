package com.jxz666.slgking.ui

import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.saveable.SaverScope
import org.junit.Assert.*
import org.junit.Test

class OverlayStackTest {
    @Test fun RotationPreservesEachEntryIdentityAndBackTarget() {
        val original = mutableStateListOf(
            OverlayEntry(Overlay.Detail(42), "detail-a"),
            OverlayEntry(Overlay.Comments("a-game", "A Game"), "comments"),
            OverlayEntry(Overlay.Detail(7), "detail-b"),
        )
        val scope = object : SaverScope { override fun canBeSaved(value: Any) = true }
        val saved = with(OverlayStackSaver) { scope.save(original) }!!
        val restored = OverlayStackSaver.restore(saved)!!
        assertEquals(original.toList(), restored.toList())
        restored.removeAt(restored.lastIndex)
        assertEquals("comments", restored.last().id)
        restored.removeAt(restored.lastIndex)
        assertEquals("detail-a", restored.last().id)
        assertEquals(Overlay.Detail(42), restored.last().overlay)
    }
    @Test fun ASecondVisitToTheSameGameGetsItsOwnScrollIdentity() {
        val first = OverlayEntry(Overlay.Detail(42))
        val second = OverlayEntry(Overlay.Detail(42))
        assertNotEquals(first.id, second.id)
    }
}
