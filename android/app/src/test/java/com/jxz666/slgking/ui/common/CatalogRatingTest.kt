package com.jxz666.slgking.ui.common

import com.jxz666.slgking.bridge.GameItem
import org.junit.Assert.assertEquals
import org.junit.Test

class CatalogRatingTest {
    private fun item(catalog: String, rating: Double?, scale: Double? = null) = GameItem(
        id = 1, slug = "test", title = "Test", titleZh = null, rating = rating,
        status = null, myRating = null, version = null, developer = null,
        engine = null, cover = null, lastUpdated = null, catalogId = catalog, ratingScaleMax = scale,
    )
    @Test fun dlsiteRatingKeepsItsNativeScale() {
        assertEquals("4.6 / 5", cardRatingLabel(item("dlsite", 4.6, 5.0)))
        assertEquals("92.0 / 100", cardRatingLabel(item("dlsite", 92.0, 100.0)))
    }
    @Test fun unknownScaleIsNotInvented() {
        assertEquals("4.6 / ?", cardRatingLabel(item("dlsite", 4.6)))
    }
    @Test fun unknownRatingIsExplicit() {
        assertEquals("未知", cardRatingLabel(item("dlsite", null, 5.0)))
    }
    @Test fun legacySlgRatingStaysUnchanged() {
        assertEquals("8.4", cardRatingLabel(item("western_slg", 8.4)))
    }
}
