package id.frostfire.apa.data

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * The offline buffer (blueprint 4.1): a notification is stored here first and sent when the network allows, so
 * nothing is lost in a tunnel or a flat battery. Only text that has ALREADY been scrubbed of personal identifiers
 * is stored, inside the app's private storage (backups are off in the manifest).
 */
class Outbox(context: Context) : SQLiteOpenHelper(context.applicationContext, "outbox.db", null, 1) {
    data class Row(val id: Long, val key: String, val pkg: String, val title: String?, val text: String, val postedAt: Long)

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT NOT NULL UNIQUE, pkg TEXT NOT NULL, title TEXT,
                text TEXT NOT NULL, posted_at INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL)""",
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    /** False when the same notification (same key) is already stored: a repost must not be sent twice. */
    fun insert(key: String, pkg: String, title: String?, text: String, postedAt: Long): Boolean {
        val v = ContentValues().apply {
            put("k", key); put("pkg", pkg); put("title", title); put("text", text)
            put("posted_at", postedAt); put("created", System.currentTimeMillis())
        }
        return writableDatabase.insertWithOnConflict("outbox", null, v, SQLiteDatabase.CONFLICT_IGNORE) != -1L
    }

    fun pending(limit: Int = 50): List<Row> = readableDatabase.rawQuery(
        "SELECT id, k, pkg, title, text, posted_at FROM outbox WHERE sent = 0 ORDER BY id LIMIT ?", arrayOf(limit.toString()),
    ).use { c ->
        buildList {
            while (c.moveToNext()) add(Row(c.getLong(0), c.getString(1), c.getString(2), c.getString(3), c.getString(4), c.getLong(5)))
        }
    }

    fun markSent(id: Long) {
        writableDatabase.execSQL("UPDATE outbox SET sent = 1 WHERE id = ?", arrayOf(id))
    }

    /** Sent rows are kept a week (to ignore a late repost with the same key), then removed. */
    fun purgeSentOlderThan(millis: Long) {
        writableDatabase.execSQL("DELETE FROM outbox WHERE sent = 1 AND created < ?", arrayOf(System.currentTimeMillis() - millis))
    }

    fun pendingCount(): Int = readableDatabase.rawQuery("SELECT COUNT(*) FROM outbox WHERE sent = 0", null).use {
        if (it.moveToFirst()) it.getInt(0) else 0
    }
}
