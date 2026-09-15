"use strict";

/** Count how often each word appears, in the order the words are first seen. */
function countWords(text) {
  const counts = new Map();
  for (const word of text.split(/\s+/).filter((item) => item.length > 0)) {
    counts.set(word, (counts.get(word) || 0) + 1);
  }
  return counts;
}

module.exports = { countWords };
