//! Fuzzy matching of spreadsheet names against the members on record.
//!
//! Titles are stripped and names normalised, then each candidate is scored by
//! how well every one of *its* name parts matches some part of the sheet's
//! name — so "Ann Wanjiru Kamau" still lines up with "Ann Kamau", and an exact
//! first name cannot outrank a better match on the whole name.
//!
//! Similarity is Python's `difflib.SequenceMatcher(None, a, b).ratio()`,
//! reproduced exactly, so names are matched as LoanMaster 1.x matched them.

/// The share of two strings that matches: `2·M / (len a + len b)`, where `M`
/// is the size of the matching blocks Ratcliff–Obershelp finds.
pub fn ratio(a: &str, b: &str) -> f64 {
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }
    2.0 * matched(&a, &b) as f64 / total as f64
}

/// Characters in the matching blocks, found as difflib finds them: the
/// longest common run (earliest in `a`, then in `b`, on a tie), then the same
/// on each side of it.
fn matched(a: &[char], b: &[char]) -> usize {
    let mut total = 0;
    let mut queue = vec![(0, a.len(), 0, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest_match(a, b, alo, ahi, blo, bhi);
        if k == 0 {
            continue;
        }
        total += k;
        if alo < i && blo < j {
            queue.push((alo, i, blo, j));
        }
        if i + k < ahi && j + k < bhi {
            queue.push((i + k, ahi, j + k, bhi));
        }
    }
    total
}

fn longest_match(a: &[char], b: &[char], alo: usize, ahi: usize, blo: usize, bhi: usize) -> (usize, usize, usize) {
    let (mut best_i, mut best_j, mut best) = (alo, blo, 0);
    // Length of the run ending at (i - 1, j), by j.
    let mut previous = vec![0usize; b.len() + 1];
    for (i, ai) in a.iter().enumerate().take(ahi).skip(alo) {
        let mut current = vec![0usize; b.len() + 1];
        for (j, bj) in b.iter().enumerate().take(bhi).skip(blo) {
            if ai == bj {
                let k = if j > blo { previous[j - 1] } else { 0 } + 1;
                current[j] = k;
                if k > best {
                    (best_i, best_j, best) = (i + 1 - k, j + 1 - k, k);
                }
            }
        }
        previous = current;
    }
    (best_i, best_j, best)
}

/// A name lowercased, without a leading title, dots or apostrophes.
pub fn normalize(name: &str) -> String {
    let mut text = name.trim();
    // Longer titles first, as the regex 1.x used backtracks: "Mrs" is not "Mr".
    for title in ["miss", "mrs", "mr", "ms", "dr"] {
        let n = title.len();
        if text.len() > n && text.as_bytes()[..n].eq_ignore_ascii_case(title.as_bytes()) {
            let rest = &text[n..];
            let rest = rest.strip_prefix('.').unwrap_or(rest);
            if rest.starts_with(char::is_whitespace) {
                text = rest;
                break;
            }
        }
    }
    let text = text.replace('.', " ").replace('\'', "").to_lowercase();
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn tokens(name: &str) -> Vec<String> {
    normalize(name).split(' ').filter(|t| !t.is_empty()).map(String::from).collect()
}

/// `(mean similarity of the candidate's parts, parts that align)` — a part
/// aligns at 0.80 or better.
pub fn score(source: &str, candidate: &[String]) -> (f64, usize) {
    let source = tokens(source);
    if source.is_empty() || candidate.is_empty() {
        return (0.0, 0);
    }
    let per: Vec<f64> =
        candidate.iter().map(|d| source.iter().map(|s| ratio(d, s)).fold(f64::NEG_INFINITY, f64::max)).collect();
    let aligned = per.iter().filter(|p| **p >= 0.80).count();
    (per.iter().sum::<f64>() / per.len() as f64, aligned)
}

/// How sure a match is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    /// Two or more name parts align.
    Match,
    /// One part aligns and the whole scores 0.66 or better: a person should look.
    Review,
    /// No confident candidate.
    None,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    pub id: i64,
    pub name: String,
    pub score: f64,
    pub aligned: usize,
}

/// Matches names against a fixed roster of `(id, name)`.
pub struct NameMatcher {
    roster: Vec<(i64, String, Vec<String>)>,
}

impl NameMatcher {
    pub fn new(roster: impl IntoIterator<Item = (i64, String)>) -> NameMatcher {
        NameMatcher { roster: roster.into_iter().map(|(id, name)| { let t = tokens(&name); (id, name, t) }).collect() }
    }

    /// The best candidate: highest score, then most aligned parts, then — as
    /// 1.x sorted — the highest id.
    pub fn best(&self, source: &str) -> Option<Candidate> {
        self.roster
            .iter()
            .filter(|(_, _, t)| !t.is_empty())
            .map(|(id, name, t)| {
                let (score, aligned) = score(source, t);
                Candidate { id: *id, name: name.clone(), score, aligned }
            })
            .max_by(|x, y| {
                x.score
                    .total_cmp(&y.score)
                    .then(x.aligned.cmp(&y.aligned))
                    .then(x.id.cmp(&y.id))
                    .then(x.name.cmp(&y.name))
            })
    }

    pub fn classify(&self, source: &str) -> (Verdict, Option<Candidate>) {
        let Some(best) = self.best(source) else { return (Verdict::None, None) };
        let verdict = if best.aligned >= 2 {
            Verdict::Match
        } else if best.aligned == 1 && best.score >= 0.66 {
            Verdict::Review
        } else {
            Verdict::None
        };
        (verdict, Some(best))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn similarity_is_difflibs() {
        // Each figure is Python's difflib.SequenceMatcher(None, a, b).ratio().
        for (a, b, want) in [
            ("abcd", "bcde", 0.75),
            ("wanjiru", "wanjiku", 0.857_142_857_142_857_1),
            ("mwangi", "mwangi", 1.0),
            ("kamau", "kimani", 0.545_454_545_454_545_5),
            ("john", "jon", 0.857_142_857_142_857_1),
            ("abcabc", "cbacba", 0.5),
            ("", "", 1.0),
            ("ann", "", 0.0),
        ] {
            assert!((ratio(a, b) - want).abs() < 1e-12, "{a} {b}: {} vs {want}", ratio(a, b));
        }
    }

    #[test]
    fn titles_dots_and_apostrophes_do_not_count() {
        assert_eq!(normalize("  Mr. John  O'Neil Jr. "), "john oneil jr");
        assert_eq!(normalize("Dr Ann"), "ann");
        assert_eq!(normalize("Mrsanne"), "mrsanne"); // a name, not a title
        assert_eq!(normalize("Mrs Ann"), "ann");
        assert_eq!(normalize("İBRAHİM Ömer"), "i̇brahi̇m ömer");
    }

    #[test]
    fn a_fuller_name_matches_and_a_stranger_does_not() {
        let roster = NameMatcher::new([(1, "ANN KAMAU".to_string()), (2, "PETER MWANGI".to_string())]);
        // Verdicts and scores as LoanMaster 1.x's matcher gives them.
        for (name, verdict, id, score) in [
            ("Ann Wanjiru Kamau", Verdict::Match, 1, 1.0),
            ("Peter Mwai", Verdict::Match, 2, 0.9),
            ("Peter Wangari", Verdict::Review, 2, 0.884_615_384_615_384_6),
            ("Peter Waweru", Verdict::Review, 2, 0.666_666_666_666_666_6),
            ("Pete Maina", Verdict::Review, 2, 0.717_171_717_171_717_1),
            ("Peter Otieno", Verdict::None, 2, 0.583_333_333_333_333_4),
            ("Zakayo Barasa", Verdict::None, 1, 0.292_929_292_929_292_93),
        ] {
            let (got, best) = roster.classify(name);
            let best = best.unwrap();
            assert_eq!((got, best.id), (verdict, id), "{name}");
            assert!((best.score - score).abs() < 1e-12, "{name}: {}", best.score);
        }
    }
}
