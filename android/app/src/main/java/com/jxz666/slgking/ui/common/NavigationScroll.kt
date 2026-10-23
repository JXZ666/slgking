package com.jxz666.slgking.ui.common

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue

internal fun shouldResetScroll(epoch: Int, consumed: Int) = epoch != consumed

/** A tab activation is consumed once. Recomposition and rotation do not reset scroll. */
@Composable
fun ConsumeScrollReset(epoch: Int, reset: suspend () -> Unit) {
    var consumed by rememberSaveable { mutableStateOf(0) }
    LaunchedEffect(epoch) {
        if (shouldResetScroll(epoch, consumed)) {
            reset()
            consumed = epoch
        }
    }
}
