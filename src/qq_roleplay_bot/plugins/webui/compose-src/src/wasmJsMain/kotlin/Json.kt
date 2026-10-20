// A ~120-line JSON reader.
//
// Why not kotlinx.serialization: this bundle is deliberately kept to Miuix + Compose (the round's
// dependency discipline is "only the modules we actually need"), and the panel only needs to walk
// three small read-only payloads (`/api/overview`, `/api/self`, `/api/model`, `/api/plugins`).
// Adding a serialization compiler plugin for that is more risk (new plugin, new wasm klib) than a
// recursive-descent reader this size.
//
// Value model: object → Map<String, Any?>, array → List<Any?>, number → Double,
// string → String, true/false → Boolean, null → null.

class JsonError(message: String) : Exception(message)

fun parseJson(text: String): Any? = JsonParser(text).run {
    val value = readValue()
    skipSpace()
    if (pos != text.length) throw JsonError("trailing data at $pos")
    value
}

private class JsonParser(private val src: String) {
    var pos = 0

    fun skipSpace() {
        while (pos < src.length && src[pos].isWhitespace()) pos++
    }

    fun readValue(): Any? {
        skipSpace()
        if (pos >= src.length) throw JsonError("unexpected end of input")
        return when (val ch = src[pos]) {
            '{' -> readObject()
            '[' -> readArray()
            '"' -> readString()
            't', 'f' -> readBoolean()
            'n' -> readNull()
            else -> if (ch == '-' || ch.isDigit()) readNumber() else throw JsonError("unexpected '$ch' at $pos")
        }
    }

    private fun readObject(): Map<String, Any?> {
        pos++ // '{'
        val out = LinkedHashMap<String, Any?>()
        skipSpace()
        if (pos < src.length && src[pos] == '}') { pos++; return out }
        while (true) {
            skipSpace()
            if (pos >= src.length || src[pos] != '"') throw JsonError("expected key at $pos")
            val key = readString()
            skipSpace()
            if (pos >= src.length || src[pos] != ':') throw JsonError("expected ':' at $pos")
            pos++
            out[key] = readValue()
            skipSpace()
            if (pos >= src.length) throw JsonError("unterminated object")
            when (src[pos]) {
                ',' -> pos++
                '}' -> { pos++; return out }
                else -> throw JsonError("expected ',' or '}' at $pos")
            }
        }
    }

    private fun readArray(): List<Any?> {
        pos++ // '['
        val out = ArrayList<Any?>()
        skipSpace()
        if (pos < src.length && src[pos] == ']') { pos++; return out }
        while (true) {
            out += readValue()
            skipSpace()
            if (pos >= src.length) throw JsonError("unterminated array")
            when (src[pos]) {
                ',' -> pos++
                ']' -> { pos++; return out }
                else -> throw JsonError("expected ',' or ']' at $pos")
            }
        }
    }

    private fun readString(): String {
        pos++ // opening quote
        val sb = StringBuilder()
        while (true) {
            if (pos >= src.length) throw JsonError("unterminated string")
            when (val ch = src[pos]) {
                '"' -> { pos++; return sb.toString() }
                '\\' -> {
                    pos++
                    if (pos >= src.length) throw JsonError("unterminated escape")
                    when (val esc = src[pos]) {
                        '"' -> sb.append('"')
                        '\\' -> sb.append('\\')
                        '/' -> sb.append('/')
                        'b' -> sb.append('\b')
                        'f' -> sb.append('\u000C')
                        'n' -> sb.append('\n')
                        'r' -> sb.append('\r')
                        't' -> sb.append('\t')
                        'u' -> {
                            val hex = src.substring(pos + 1, minOf(pos + 5, src.length))
                            val code = hex.toIntOrNull(16) ?: throw JsonError("bad \\u escape")
                            sb.append(code.toChar())
                            pos += 4
                        }
                        else -> throw JsonError("bad escape '\\$esc'")
                    }
                    pos++
                }
                else -> { sb.append(ch); pos++ }
            }
        }
    }

    private fun readBoolean(): Boolean = when {
        src.startsWith("true", pos) -> { pos += 4; true }
        src.startsWith("false", pos) -> { pos += 5; false }
        else -> throw JsonError("bad literal at $pos")
    }

    private fun readNull(): Any? {
        if (!src.startsWith("null", pos)) throw JsonError("bad literal at $pos")
        pos += 4
        return null
    }

    private fun readNumber(): Double {
        val start = pos
        if (pos < src.length && src[pos] == '-') pos++
        while (pos < src.length && (src[pos].isDigit() || src[pos] in ".eE+-")) pos++
        return src.substring(start, pos).toDoubleOrNull() ?: throw JsonError("bad number at $start")
    }
}

// --- small typed accessors -------------------------------------------------
// The panel's payloads are as untrusted as any other HTTP body: every read is defaulting, so a
// missing or wrongly-typed field degrades that one row instead of throwing the page away.

fun Any?.asObj(): Map<String, Any?>? = this as? Map<String, Any?>

fun Any?.asList(): List<Any?> = (this as? List<Any?>) ?: emptyList()

fun Any?.field(name: String): Any? = asObj()?.get(name)

fun Any?.string(name: String, fallback: String = ""): String = when (val v = field(name)) {
    is String -> v
    is Double -> if (v == v.toLong().toDouble()) v.toLong().toString() else v.toString()
    is Boolean -> v.toString()
    else -> fallback
}

fun Any?.number(name: String, fallback: Double = 0.0): Double = field(name) as? Double ?: fallback

fun Any?.bool(name: String, fallback: Boolean = false): Boolean = field(name) as? Boolean ?: fallback

fun Any?.stringList(name: String): List<String> = field(name).asList().mapNotNull { it as? String }
