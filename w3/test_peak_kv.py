"""
GPU 없이 peak-KV 예약 논리(_peak_kv_fits)를 확인하는 단위 테스트. 패치를 적용한 뒤 실행한다.

    python w3/test_peak_kv.py

1) 손으로 계산한 경우 몇 개 (idle 서버는 항상 받는다, 끝난 요청 무시, 공유 prefix 보정, α)
2) α=1, 공유 prefix 없음, page_size=1 이면 SGLang 기존 add_one_req_ignore_eos 의
   시뮬레이션과 같은 판정을 내리는지 무작위 2,000 상태로 대조
"""

import random
import sys

try:
    from sglang.srt.managers.schedule_policy import PrefillAdder
except Exception as e:  # noqa: BLE001
    sys.exit(f"sglang import 실패: {e}")
if not hasattr(PrefillAdder, "_peak_kv_fits"):
    sys.exit("패치가 적용되지 않았다 (PrefillAdder._peak_kv_fits 없음). 가이드 1의 3.2 참고.")

F = PrefillAdder._peak_kv_fits


class SP:
    def __init__(self, m):
        self.max_new_tokens = m


class R:
    def __init__(self, inp, out, maxnew, prefix=0, cached=0, fin=False):
        self.origin_input_ids = [0] * inp
        self.output_ids = [0] * out
        self.sampling_params = SP(maxnew)
        self.prefix_indices = [0] * prefix
        self.cached_tokens = cached
        self._fin = fin

    def finished(self):
        return self._fin


class B:
    def __init__(self, reqs):
        self.reqs = reqs


class Fake:
    def __init__(self, running, can_run, free, ratio=1.0, page=1):
        self.running_batch = B(running)
        self.can_run_list = can_run
        self.cur_rem_tokens = free
        self.peak_kv_reserve_ratio = ratio
        self.page_size = page

    def ceil_paged_tokens(self, t):
        return -(-t // self.page_size) * self.page_size


def main():
    cand = R(100, 0, 400)
    assert F(Fake([], [], 10), cand, 100) is True, "idle 서버는 항상 1개를 받아야 한다 (deadlock 방지)"
    assert F(Fake([R(100, 0, 500)], [], 1000), cand, 100) is True
    assert F(Fake([R(100, 0, 500)], [], 700), cand, 100) is False
    assert F(Fake([R(100, 0, 500)], [], 700, ratio=0.5), cand, 100) is True, "α 가 작으면 덜 예약"
    assert F(Fake([R(100, 500, 500, fin=True)], [], 10), cand, 100) is True, "끝난 요청은 무시"
    a, c = R(200, 0, 10, cached=150), R(100, 0, 300)
    assert F(Fake([a], [], 500), c, 100) is True
    assert F(Fake([a], [], 300), c, 100) is False, "공유 prefix 는 끝나도 풀리지 않는다"

    def upstream(states, cur_rem):          # add_one_req_ignore_eos 의 루프 그대로
        freed = 0
        for i, (left, occ) in enumerate(sorted(states, key=lambda x: x[0])):
            bs = len(states) - i
            if cur_rem + freed - left * bs <= 1 * bs:
                return False
            freed += occ
        return True

    rnd = random.Random(0)
    for _ in range(2000):
        run = [R(rnd.randint(50, 500), rnd.randint(0, 3000), 0) for _ in range(rnd.randint(1, 30))]
        for r in run:
            r.sampling_params.max_new_tokens = len(r.output_ids) + rnd.randint(1, 8000)
        cand = R(rnd.randint(50, 500), 0, rnd.randint(256, 16000))
        free = rnd.randint(0, 90000)
        ext = len(cand.origin_input_ids)
        st = [(r.sampling_params.max_new_tokens - len(r.output_ids),
               len(r.origin_input_ids) + len(r.output_ids)) for r in run + [cand]]
        assert F(Fake(run, [], free), cand, ext) == upstream(st, free - ext)
    print("all peak-KV unit tests passed")


def bench():
    """보고서 5.1 CPU 오버헤드: 실행 중 요청 수별 _peak_kv_fits 1회 호출 시간."""
    import time
    rnd = random.Random(1)
    for n in (8, 24, 48, 84, 128):
        run = [R(387, rnd.randint(0, 3000), 0, prefix=36, cached=36) for _ in range(n)]
        for r in run:
            r.sampling_params.max_new_tokens = len(r.output_ids) + rnd.randint(1, 8000)
        f, cand = Fake(run, [], 10**9), R(387, 0, 3000, prefix=36)
        t, N = time.perf_counter(), 2000
        for _ in range(N):
            F(f, cand, 351)
        print(f"#running-req={n:4d}  {1e6 * (time.perf_counter() - t) / N:7.1f} us / 호출")


if __name__ == "__main__":
    main()
    if "--bench" in sys.argv:
        bench()
