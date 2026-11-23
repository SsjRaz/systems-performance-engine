// Copy into crates/filter-parser/examples/ in either v1.42.0 source tree.
use filter_parser::{FilterCondition, IndexFilterCondition};
use std::alloc::{GlobalAlloc, Layout, System};
use std::hint::black_box;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};

struct Counter;
static CALLS: AtomicUsize = AtomicUsize::new(0);
static BYTES: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counter {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        CALLS.fetch_add(1, Relaxed);
        BYTES.fetch_add(layout.size(), Relaxed);
        unsafe { System.alloc(layout) }
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        CALLS.fetch_add(1, Relaxed);
        BYTES.fetch_add(layout.size(), Relaxed);
        unsafe { System.alloc_zeroed(layout) }
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        CALLS.fetch_add(1, Relaxed);
        BYTES.fetch_add(size, Relaxed);
        unsafe { System.realloc(ptr, layout, size) }
    }
}
#[global_allocator]
static ALLOCATOR: Counter = Counter;

fn main() {
    for (case, source) in [
        ("equality", "sku = 'tea'"),
        ("empty", "sku IN []"),
        ("one", "sku IN ['tea']"),
        ("eight", "sku IN ['tea', 'rye', 'oats', 'rice', 'fig', 'yam', 'pea', 'plum']"),
    ] {
        let parsed = FilterCondition::parse(source).unwrap().unwrap();
        let condition = match parsed {
            FilterCondition::Condition { fid, op } => IndexFilterCondition::Condition { fid, op },
            FilterCondition::In { fid, els } => IndexFilterCondition::In { fid, els },
            _ => unreachable!(),
        };
        let expected = condition.to_string();
        let start_calls = CALLS.load(Relaxed);
        let start_bytes = BYTES.load(Relaxed);
        let owned = black_box(black_box(condition).into_owned());
        let calls = CALLS.load(Relaxed) - start_calls;
        let bytes = BYTES.load(Relaxed) - start_bytes;
        assert_eq!(owned.to_string(), expected);
        if let IndexFilterCondition::In { fid, els } = &owned {
            assert_eq!(fid.extra(), source);
            assert!(els.iter().all(|el| el.extra() == source));
        }
        println!("{case}\t{calls}\t{bytes}");
    }
}
