"""The CSV replay example's consumer: it shows what a subscriber receives.

It subscribes to every replayed Channel and republishes each sample it
receives on `csv.seen`: the sample's own publish time, which Channel it came
from, and its value. The Run Recording then holds the consumer's view of the
replay, in the order the consumer received it, so a reader can compare it with
the CSV rows directly.

Channels are numbered in name order, from the init line, so the observer
needs no configuration. Each replayed schema of this example has exactly one
field, and that field is the value.
"""

from sil.participant import StepParticipant, run


class Observer(StepParticipant):
    def on_init(self, init):
        inputs = sorted(name for name, channel in init["channels"].items()
                        if channel["direction"] == "in")
        self._number = {name: index for index, name in enumerate(inputs)}

    def on_step(self, t, dt, inputs):
        return [("csv.seen", {
            "sample_ns": sample.publish_ns,
            "channel": self._number[sample.channel],
            "value": float(*sample.data.values()),
        }) for sample in inputs]


if __name__ == "__main__":
    run(Observer())
