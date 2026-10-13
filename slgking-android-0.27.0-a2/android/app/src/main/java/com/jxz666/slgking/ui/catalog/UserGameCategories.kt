package com.jxz666.slgking.ui.catalog

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

val USER_GAME_CATEGORIES = listOf(
    "galgame" to "Galgame", "slg" to "SLG", "rpg" to "RPG", "act" to "ACT",
    "simulation" to "模拟经营", "casual_puzzle" to "休闲/解谜", "other" to "其他",
)

fun categorySummary(ids: List<String>): String =
    USER_GAME_CATEGORIES.filter { it.first in ids }.joinToString(" · ") { it.second }.ifBlank { "未分类" }

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CategoryChips(selected: Set<String>, enabled: Boolean = true, onChanged: (Set<String>) -> Unit) {
    FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        USER_GAME_CATEGORIES.forEach { (id, label) ->
            FilterChip(selected = id in selected, enabled = enabled,
                onClick = { onChanged(if (id in selected) selected - id else selected + id) },
                label = { Text(label) })
        }
    }
}
