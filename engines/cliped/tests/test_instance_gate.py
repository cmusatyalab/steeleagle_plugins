import numpy as np

from cliped.instance_gate import InstanceGate

T = np.array([0.0, 1.0, 0.0])
C = np.array([0.6, 0.6, 0.53])


def test_it_doesnt_cut_until_it_has_a_yes_and_a_no():
    gate = InstanceGate(T, C)
    gate.learn(np.array([0.0, 1.0, 0.0]), True)
    assert gate.cutoff is None and gate.passes(-5.0)
    gate.learn(np.array([0.0, 0.0, 1.0]), False)
    assert gate.cutoff is not None


def test_it_cuts_at_the_midpoint_of_the_yes_and_no_margins():
    gate = InstanceGate(T, C)
    yes, no = np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, 1.0])
    gate.learn(yes, True)
    gate.learn(no, False)
    m_yes, m_no = gate.margins(yes)[0], gate.margins(no)[0]
    assert np.isclose(gate.cutoff, (m_yes + m_no) / 2)
    assert gate.passes(m_yes) and not gate.passes(m_no)


def test_it_doesnt_cut_while_the_yes_margins_are_no_higher():
    gate = InstanceGate(T, C)
    gate.learn(np.array([0.0, 0.0, 1.0]), True)
    gate.learn(np.array([0.0, 1.0, 0.0]), False)
    assert gate.cutoff is None


def test_it_keeps_the_latest_answers():
    gate = InstanceGate(T, C, max_answers=3)
    for _ in range(5):
        gate.learn(np.array([0.0, 1.0, 0.0]), True)
    assert len(gate.yes) == 3
