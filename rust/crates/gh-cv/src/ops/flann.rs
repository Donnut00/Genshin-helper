//! 4-tree randomized KD-Tree (`KdTreeIndex`, `checks=48`, L2 `f32`, `rayon`-parallel)
//! + brute-force `bf_knn_match_l2` and Lowe's ratio helper.

use crate::ops::{CvError, CvResult, DMatch};
use rayon::prelude::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

#[derive(Debug, Clone, Copy)]
struct OrdF32(f32);

impl PartialEq for OrdF32 {
    fn eq(&self, other: &Self) -> bool {
        self.0.to_bits() == other.0.to_bits()
    }
}
impl Eq for OrdF32 {}
impl PartialOrd for OrdF32 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for OrdF32 {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0.total_cmp(&other.0)
    }
}

#[derive(Debug, Clone)]
enum KdNode {
    Internal {
        split_dim: u8,
        split_val: f32,
        left: usize,
        right: usize,
    },
    Leaf {
        start: usize,
        end: usize,
    },
}

#[derive(Debug, Clone)]
struct KdTree {
    nodes: Vec<KdNode>,
    ordered_indices: Vec<usize>,
}

#[derive(Debug, Clone)]
pub struct KdTreeIndex {
    descriptors: Vec<[f32; 128]>,
    trees: Vec<KdTree>,
    checks: usize,
}

#[inline]
fn splitmix64(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9e37_79b9_7f4a_7c15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

#[inline]
fn l2_sq_128(a: &[f32; 128], b: &[f32; 128]) -> f32 {
    let mut s0 = 0.0f32;
    let mut s1 = 0.0f32;
    let mut s2 = 0.0f32;
    let mut s3 = 0.0f32;
    for i in (0..128).step_by(4) {
        let d0 = a[i] - b[i];
        let d1 = a[i + 1] - b[i + 1];
        let d2 = a[i + 2] - b[i + 2];
        let d3 = a[i + 3] - b[i + 3];
        s0 += d0 * d0;
        s1 += d1 * d1;
        s2 += d2 * d2;
        s3 += d3 * d3;
    }
    (s0 + s1) + (s2 + s3)
}

fn build_single_tree(descriptors: &[[f32; 128]], seed: u64) -> KdTree {
    const LEAF_SIZE: usize = 16;
    let n = descriptors.len();
    let mut ordered_indices: Vec<usize> = (0..n).collect();
    let mut nodes = Vec::with_capacity((n / LEAF_SIZE) * 2 + 4);
    let mut rng = seed;

    fn build_rec(
        descriptors: &[[f32; 128]],
        indices: &mut [usize],
        offset: usize,
        nodes: &mut Vec<KdNode>,
        rng: &mut u64,
    ) -> usize {
        if indices.len() <= LEAF_SIZE {
            let id = nodes.len();
            nodes.push(KdNode::Leaf {
                start: offset,
                end: offset + indices.len(),
            });
            return id;
        }

        // Sample up to 128 points to pick one of top-5 max-variance dimensions
        let sample_n = indices.len().min(128);
        let step = (indices.len() / sample_n).max(1);
        let mut means = [0.0f64; 128];
        let mut vars = [0.0f64; 128];
        let mut cnt = 0usize;
        for &idx in indices.iter().step_by(step).take(sample_n) {
            let d = &descriptors[idx];
            for j in 0..128 {
                means[j] += d[j] as f64;
            }
            cnt += 1;
        }
        let inv_c = 1.0 / (cnt.max(1) as f64);
        for m in &mut means {
            *m *= inv_c;
        }
        for &idx in indices.iter().step_by(step).take(sample_n) {
            let d = &descriptors[idx];
            for j in 0..128 {
                let diff = d[j] as f64 - means[j];
                vars[j] += diff * diff;
            }
        }

        let mut dims: [usize; 128] = std::array::from_fn(|i| i);
        dims.sort_by(|&a, &b| vars[b].total_cmp(&vars[a]));
        let top_k = 5usize;
        let pick = (splitmix64(rng) as usize) % top_k;
        let split_dim = dims[pick];

        if vars[split_dim] <= 1e-12 {
            let id = nodes.len();
            nodes.push(KdNode::Leaf {
                start: offset,
                end: offset + indices.len(),
            });
            return id;
        }

        let mid = indices.len() / 2;
        indices.select_nth_unstable_by(mid, |&a, &b| {
            descriptors[a][split_dim].total_cmp(&descriptors[b][split_dim])
        });
        let split_val = descriptors[indices[mid]][split_dim];

        // Avoid degenerate zero-side splits if all elements equal split_val
        let (left_slice, right_slice) = indices.split_at_mut(mid);
        if left_slice.is_empty() || right_slice.is_empty() {
            let id = nodes.len();
            nodes.push(KdNode::Leaf {
                start: offset,
                end: offset + indices.len(),
            });
            return id;
        }

        let node_id = nodes.len();
        nodes.push(KdNode::Leaf { start: 0, end: 0 });
        let left_id = build_rec(descriptors, left_slice, offset, nodes, rng);
        let right_id = build_rec(descriptors, right_slice, offset + mid, nodes, rng);
        nodes[node_id] = KdNode::Internal {
            split_dim: split_dim as u8,
            split_val,
            left: left_id,
            right: right_id,
        };
        node_id
    }

    build_rec(descriptors, &mut ordered_indices, 0, &mut nodes, &mut rng);
    KdTree {
        nodes,
        ordered_indices,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct QueueEntry {
    dist_sq: OrdF32,
    tree_id: usize,
    node_id: usize,
}

impl Ord for QueueEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        // Min-heap by dist_sq (Reverse ordering)
        other.dist_sq.cmp(&self.dist_sq)
    }
}

impl PartialOrd for QueueEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl KdTreeIndex {
    /// Builds a randomized KD-Tree index (`trees=4`, `checks=48` for OpenCV FLANN parity).
    pub fn build(descriptors: Vec<[f32; 128]>, num_trees: usize, checks: usize) -> CvResult<Self> {
        if descriptors.is_empty() {
            return Err(CvError::EmptyInput("KdTreeIndex empty descriptors".into()));
        }
        let n_trees = num_trees.max(1);
        let trees: Vec<KdTree> = (0..n_trees)
            .into_par_iter()
            .map(|t| {
                build_single_tree(
                    &descriptors,
                    0x9e37_79b9_7f4a_7c15 ^ ((t as u64 + 1).wrapping_mul(0xbf58_476d_1ce4_e5b9)),
                )
            })
            .collect();
        Ok(Self {
            descriptors,
            trees,
            checks: checks.max(1),
        })
    }

    pub fn len(&self) -> usize {
        self.descriptors.len()
    }

    pub fn is_empty(&self) -> bool {
        self.descriptors.is_empty()
    }

    /// Queries `k` nearest neighbors for a single 128-D descriptor.
    pub fn knn_query_one(&self, query: &[f32; 128], k: usize, query_idx: usize) -> Vec<DMatch> {
        if k == 0 || self.descriptors.is_empty() {
            return Vec::new();
        }
        let mut pq = BinaryHeap::<QueueEntry>::with_capacity(64);
        let mut leaf_count = 0usize;
        let mut best: Vec<(f32, usize)> = Vec::with_capacity(k + 1);
        const HT_SIZE: usize = 2048;
        const HT_MASK: usize = HT_SIZE - 1;
        const HT_SPILL_THRESHOLD: usize = 1536;
        let mut seen_ht = [usize::MAX; HT_SIZE];
        let mut seen_count = 0usize;
        let mut spill_bitset: Option<Vec<u64>> = None;
        let n_train = self.descriptors.len();

        let mark_seen = |ht: &mut [usize; HT_SIZE],
                         count: &mut usize,
                         bitset: &mut Option<Vec<u64>>,
                         val: usize|
         -> bool {
            if let Some(bits) = bitset.as_mut() {
                let word = val >> 6;
                let mask = 1u64 << (val & 63);
                if (bits[word] & mask) != 0 {
                    return false;
                }
                bits[word] |= mask;
                return true;
            }
            let mut slot =
                (((val as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15) >> 53) as usize) & HT_MASK;
            for _ in 0..HT_SIZE {
                let cur = ht[slot];
                if cur == val {
                    return false;
                }
                if cur == usize::MAX {
                    if *count >= HT_SPILL_THRESHOLD {
                        let mut bits = vec![0u64; n_train.div_ceil(64)];
                        for &entry in ht.iter() {
                            if entry != usize::MAX {
                                bits[entry >> 6] |= 1u64 << (entry & 63);
                            }
                        }
                        bits[val >> 6] |= 1u64 << (val & 63);
                        *bitset = Some(bits);
                        return true;
                    }
                    ht[slot] = val;
                    *count += 1;
                    return true;
                }
                slot = (slot + 1) & HT_MASK;
            }
            // Fallback spill if table ever filled unexpectedly
            let mut bits = vec![0u64; n_train.div_ceil(64)];
            for &entry in ht.iter() {
                if entry != usize::MAX {
                    bits[entry >> 6] |= 1u64 << (entry & 63);
                }
            }
            let word = val >> 6;
            let mask = 1u64 << (val & 63);
            let inserted = (bits[word] & mask) == 0;
            bits[word] |= mask;
            *bitset = Some(bits);
            inserted
        };

        let desc_down = |tree: &KdTree,
                           tree_id: usize,
                           mut node_id: usize,
                           pq: &mut BinaryHeap<QueueEntry>,
                           best: &mut Vec<(f32, usize)>,
                           seen_ht: &mut [usize; HT_SIZE],
                           seen_count: &mut usize,
                           spill_bitset: &mut Option<Vec<u64>>,
                           leaf_count: &mut usize| {
            loop {
                match tree.nodes[node_id] {
                    KdNode::Internal {
                        split_dim,
                        split_val,
                        left,
                        right,
                    } => {
                        let diff = query[split_dim as usize] - split_val;
                        let (near, far) = if diff < 0.0 {
                            (left, right)
                        } else {
                            (right, left)
                        };
                        let bound_sq = diff * diff;
                        let worst_sq = if best.len() == k {
                            best[k - 1].0
                        } else {
                            f32::INFINITY
                        };
                        if bound_sq < worst_sq {
                            pq.push(QueueEntry {
                                dist_sq: OrdF32(bound_sq),
                                tree_id,
                                node_id: far,
                            });
                        }
                        node_id = near;
                    }
                    KdNode::Leaf { start, end } => {
                        *leaf_count += 1;
                        for &tidx in &tree.ordered_indices[start..end] {
                            if !mark_seen(seen_ht, seen_count, spill_bitset, tidx) {
                                continue;
                            }
                            let d2 = l2_sq_128(query, &self.descriptors[tidx]);
                            if best.len() < k || d2 < best[k - 1].0 {
                                best.push((d2, tidx));
                                best.sort_by(|a, b| a.0.total_cmp(&b.0));
                                if best.len() > k {
                                    best.truncate(k);
                                }
                            }
                        }
                        break;
                    }
                }
            }
        };

        for (t_id, tree) in self.trees.iter().enumerate() {
            desc_down(
                tree,
                t_id,
                0,
                &mut pq,
                &mut best,
                &mut seen_ht,
                &mut seen_count,
                &mut spill_bitset,
                &mut leaf_count,
            );
        }

        while leaf_count < self.checks {
            let Some(entry) = pq.pop() else {
                break;
            };
            let worst_sq = if best.len() == k {
                best[k - 1].0
            } else {
                f32::INFINITY
            };
            if entry.dist_sq.0 >= worst_sq {
                break;
            }
            desc_down(
                &self.trees[entry.tree_id],
                entry.tree_id,
                entry.node_id,
                &mut pq,
                &mut best,
                &mut seen_ht,
                &mut seen_count,
                &mut spill_bitset,
                &mut leaf_count,
            );
        }

        best.into_iter()
            .map(|(d2, train_idx)| DMatch {
                query_idx,
                train_idx,
                distance: d2.sqrt(),
            })
            .collect()
    }

    /// Parallel batch `knnMatch` across all queries.
    pub fn knn_match(&self, queries: &[[f32; 128]], k: usize) -> Vec<Vec<DMatch>> {
        queries
            .par_iter()
            .enumerate()
            .map(|(qi, q)| self.knn_query_one(q, k, qi))
            .collect()
    }
}

/// Brute-force L2 k-NN matcher (`cv2.BFMatcher(NORM_L2).knnMatch`) on 128-D `f32` descriptors.
pub fn bf_knn_match_l2(
    queries: &[[f32; 128]],
    train: &[[f32; 128]],
    k: usize,
) -> Vec<Vec<DMatch>> {
    if queries.is_empty() || train.is_empty() || k == 0 {
        return vec![Vec::new(); queries.len()];
    }
    queries
        .par_iter()
        .enumerate()
        .map(|(qi, q)| {
            let mut best: Vec<(f32, usize)> = Vec::with_capacity(k + 1);
            for (ti, t) in train.iter().enumerate() {
                let d2 = l2_sq_128(q, t);
                if best.len() < k || d2 < best[k - 1].0 {
                    best.push((d2, ti));
                    best.sort_by(|a, b| a.0.total_cmp(&b.0));
                    if best.len() > k {
                        best.truncate(k);
                    }
                }
            }
            best.into_iter()
                .map(|(d2, train_idx)| DMatch {
                    query_idx: qi,
                    train_idx,
                    distance: d2.sqrt(),
                })
                .collect()
        })
        .collect()
}

/// Filters `knnMatch(k=2)` pairs with Lowe's ratio test (`m0.distance < ratio * m1.distance`).
pub fn lowe_ratio_filter(pairs: &[Vec<DMatch>], ratio: f32) -> Vec<DMatch> {
    let mut good = Vec::new();
    for pair in pairs {
        if pair.len() >= 2 && pair[1].distance > 1e-6 && pair[0].distance < ratio * pair[1].distance
        {
            good.push(pair[0]);
        }
    }
    good
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kdtree_recalls_planted_neighbors_and_matches_bf() {
        let mut state = 123456789u64;
        let mut train = Vec::with_capacity(500);
        for _ in 0..500 {
            let mut v = [0.0f32; 128];
            let mut norm_sq = 0.0f32;
            for x in &mut v {
                let r = (splitmix64(&mut state) & 0xffff) as f32 / 65535.0;
                *x = r;
                norm_sq += r * r;
            }
            let inv = 1.0 / norm_sq.sqrt();
            for x in &mut v {
                *x *= inv;
            }
            train.push(v);
        }
        let idx = KdTreeIndex::build(train.clone(), 4, 48).unwrap();
        let mut queries = Vec::new();
        for &ti in &[17usize, 123, 399] {
            let mut q = train[ti];
            q[0] += 0.01;
            queries.push(q);
        }
        let kd_res = idx.knn_match(&queries, 2);
        let bf_res = bf_knn_match_l2(&queries, &train, 2);
        for (i, &ti) in [17usize, 123, 399].iter().enumerate() {
            assert_eq!(kd_res[i][0].train_idx, ti);
            assert_eq!(bf_res[i][0].train_idx, ti);
        }
    }

    #[test]
    fn kdtree_handles_3000_duplicates_and_high_checks_without_hanging() {
        let base = [0.25f32; 128];
        let dup_idx = KdTreeIndex::build(vec![base; 3000], 4, 48).unwrap();
        let res_dup = dup_idx.knn_match(&[base], 2);
        assert_eq!(res_dup.len(), 1);
        assert_eq!(res_dup[0].len(), 2);
        assert_ne!(res_dup[0][0].train_idx, res_dup[0][1].train_idx);
        assert!(res_dup[0][0].distance < 1e-6);

        let mut state = 987654321u64;
        let mut distinct_4000 = Vec::with_capacity(4000);
        for _ in 0..4000 {
            let mut v = [0.0f32; 128];
            let mut norm_sq = 0.0f32;
            for x in &mut v {
                let r = (splitmix64(&mut state) & 0xffff) as f32 / 65535.0;
                *x = r;
                norm_sq += r * r;
            }
            let inv = 1.0 / norm_sq.sqrt();
            for x in &mut v {
                *x *= inv;
            }
            distinct_4000.push(v);
        }
        let hi_idx = KdTreeIndex::build(distinct_4000.clone(), 4, 200).unwrap();
        let q = distinct_4000[1337];
        let res_hi = hi_idx.knn_match(&[q], 2);
        assert_eq!(res_hi[0][0].train_idx, 1337);
        assert!(res_hi[0][0].distance < 1e-5);
    }
}
