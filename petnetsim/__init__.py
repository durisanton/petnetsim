from enum import IntEnum
import numpy as np
from operator import attrgetter
from .elements import *
# from .xml_loader import load_xml


class ConflictGroupType(IntEnum):
    Normal = 0
    Priority = 1
    Stochastic = 2
    Timed = 3


class PetriNet:
    def __init__(self, places, transitions, arcs):
        self._names_lookup = {}

        places = [Place(p) if isinstance(p, str) else p for p in places]

        for p in places:
            if p.name in self._names_lookup:
                raise RuntimeError('name reused: '+p.name)
            self._names_lookup[p.name] = p

        transitions = [Transition(t) if isinstance(t, str) else t for t in transitions]

        for t in transitions:
            if t.name in self._names_lookup:
                raise RuntimeError('name reused: '+t.name)
            self._names_lookup[t.name] = t

        arcs = [Arc(a[0], a[1]) if isinstance(a, tuple) else a for a in arcs]

        for arc in arcs:
            if arc.name in self._names_lookup:
                raise RuntimeError('name reused: '+arc.name)
            self._names_lookup[arc.name] = arc
            arc.connect(self._names_lookup)

        for t in transitions:
            t.freeze()

        self.places = tuple(places)
        self.transitions = tuple(transitions)
        self.arcs = tuple(arcs)

        self._make_conflict_groups()

        self.enabled = np.zeros(len(transitions), dtype=np.bool)
        self.enabled_tmp = np.zeros(len(transitions), dtype=np.bool)
        self._ended = False
        self.step_num = 0
        self.time = 0.0
        # fired in last step
        self.fired = []

    @property
    def ended(self):
        return self._ended

    def reset(self):
        self._ended = False
        self.step_num = 0
        self.time = 0.0
        self.fired.clear()
        self.conflict_groups_waiting.fill(0)
        for t in self.transitions:
            t.reset()
        for p in self.places:
            p.reset()

    def step(self, record_fired=True):
        if record_fired:
            self.fired.clear()
        # enabled transitions
        for ti, t in enumerate(self.transitions):
            self.enabled[ti] = t.enabled()

        CGT = ConflictGroupType

        num_fired = 0
        enabled_any = self.enabled.any()
        if enabled_any:
            np.bitwise_and(self.enabled, self.conflict_groups_mask, out=self.enabled_conflict_groups)

            for cgi, ecg in enumerate(self.enabled_conflict_groups):
                if ecg.any():
                    cg_type = self.conflict_groups_types[cgi]
                    t_idxs = np.argwhere(ecg).flatten()  # absolute indicies of enabled transitions in group
                    if cg_type == CGT.Normal:
                        t_fire_idx = np.random.choice(t_idxs)
                    elif cg_type == CGT.Priority:
                        priorities = self.conflict_group_data[cgi]
                        ep = priorities[t_idxs]
                        ep_idxs = np.argwhere(ep == ep.max()).flatten()
                        ep_idx = np.random.choice(ep_idxs)
                        t_fire_idx = t_idxs[ep_idx]
                    elif cg_type == CGT.Stochastic:
                        probabilities = self.conflict_group_data[cgi][t_idxs]
                        # "normalize" the sum of probabilities to 1
                        probabilities_norm = probabilities * (1 / np.sum(probabilities))
                        t_fire_idx = np.random.choice(t_idxs, p=probabilities_norm[t_idxs])
                    elif cg_type == CGT.Timed:
                        # conflict_group_data[cgi][0, ti] = isinstance(t, TransitionTimed)
                        # conflict_group_data[cgi][1, ti] = not isinstance(t, TransitionTimed)
                        if self.conflict_groups_waiting[cgi] <= 0:
                            normal_enabled = self.enabled_tmp
                            np.bitwise_and(ecg, self.conflict_group_data[cgi][1], out=normal_enabled)
                            if any(normal_enabled):  # use normal transition
                                normal_t_idxs = np.argwhere(normal_enabled).flatten()
                                t_fire_idx = np.random.choice(normal_t_idxs)
                            else:  # then must be timed
                                timed_enabled = self.enabled_tmp
                                np.bitwise_and(ecg, self.conflict_group_data[cgi][0], out=timed_enabled)
                                t_fire_idx = None
                                timed_t_idxs = np.argwhere(timed_enabled).flatten()
                                timed_t_idx = np.random.choice(timed_t_idxs)
                                timed_t = self.transitions[timed_t_idx]
                                self.conflict_groups_waiting[cgi] = timed_t.wait()
                                #print(' ', timed_t.name, 'wait =', self.conflict_groups_waiting[cgi])
                        else:
                            t_fire_idx = None

                    if t_fire_idx is not None:
                        t = self.transitions[t_fire_idx]
                        t.fire()
                        num_fired += 1
                        if record_fired:
                            self.fired.append(t)

        num_waiting = np.sum(self.conflict_groups_waiting > 0)

        if num_waiting > 0 and num_fired == 0:
            # nothing fired -> advance time and fire waiting timed transitions
            min_time = np.min(self.conflict_groups_waiting[self.conflict_groups_waiting>0])
            self.time += min_time

            for cgi in np.argwhere(self.conflict_groups_waiting == min_time).flatten():
                for ti in np.where(self.conflict_group_data[cgi][0])[0]:
                    t = self.transitions[ti]
                    if t.is_waiting:
                        t.fire()
                        num_fired += 1
                        if record_fired:
                            self.fired.append(t)
                        break
                self.conflict_groups_waiting[cgi] = 0

            np.subtract(self.conflict_groups_waiting, min_time, out=self.conflict_groups_waiting)
            np.clip(self.conflict_groups_waiting, 0, float('inf'), out=self.conflict_groups_waiting)

        if not enabled_any and num_waiting == 0:
            self._ended = True
        self.step_num += 1

    def print_places(self):
        for p in self.places:
            print(p.name, p.tokens, sep=': ')

    def validate(self):
        # TODO : validation of whole net
        print('TODO: PetriNet.validate')
        pass

    def _make_conflict_groups(self):

        # print('sharing the inputs and ouputs via normal arcs')
        # print('; ', end='')
        # for t1 in self.transitions[1:]:
        #     print(t1.name, end='; ')
        # print()

        groups = []

        for t1i, t1 in enumerate(self.transitions[:-1]):
            print(*[set(t.name for t in g) for g in groups])
            in_groups = [t1 in g for g in groups]
            if sum(in_groups) > 1:
                raise RuntimeError('transition can be only in one conflict group')
            to_group: set
            if not any(in_groups):
                groups.append({t1})
                to_group = groups[-1]
            else:
                to_group = groups[in_groups.index(True)]

            # print(t1.name, end='; ')
            # for _ in range(t1i):
            #     print('; ', end='')
            for t2 in self.transitions[t1i+1:]:
                if t1 is not t2:
                    # ignore inhibitors!
                    t1_in = set(arc.source for arc in t1.inputs if isinstance(arc, Arc))
                    t1_out = set(arc.target for arc in t1.outputs if isinstance(arc, Arc))
                    t2_in = set(arc.source for arc in t2.inputs if isinstance(arc, Arc))
                    t2_out = set(arc.target for arc in t2.outputs if isinstance(arc, Arc))
                    # print(not t1_in.isdisjoint(t2_in) or not t1_out.isdisjoint(t2_out),
                    #       'in:', *(x.name for x in t1_in.intersection(t2_in)),
                    #       'out:', *(x.name for x in t1_out.intersection(t2_out)),
                    #       end='; ')
                    if not t1_in.isdisjoint(t2_in) or not t1_out.isdisjoint(t2_out):
                        to_group.add(t2)

            print()



        conflict_groups_sets = [{self.transitions[0]}]
        for t in self.transitions[1:]:
            add_to_cg = False
            # print('t: ', t.name)
            for cg in conflict_groups_sets:
                for cg_t in cg:
                    # ignore inhibitors!
                    t_in = set(arc.source for arc in t.inputs if isinstance(arc, Arc))
                    t_out = set(arc.target for arc in t.outputs if isinstance(arc, Arc))
                    cg_t_in = set(arc.source for arc in cg_t.inputs if isinstance(arc, Arc))
                    cg_t_out = set(arc.target for arc in cg_t.outputs if isinstance(arc, Arc))

                    add_to_cg = add_to_cg or not t_in.isdisjoint(cg_t_in)
                    add_to_cg = add_to_cg or not t_out.isdisjoint(cg_t_out)
                    if add_to_cg:
                        break
                if add_to_cg:
                    cg.add(t)
                    break

            if not add_to_cg:
                conflict_groups_sets.append({t})

        #conflict_groups = tuple(tuple(sorted(cgs, key=attrgetter('name'))) for cgs in conflict_groups_sets)

        conflict_groups_types = [None for _ in conflict_groups_sets]

        def t_cg_type(transition):
            if isinstance(transition, TransitionPriority):
                return ConflictGroupType.Priority
            elif isinstance(transition, TransitionStochastic):
                return ConflictGroupType.Stochastic
            elif isinstance(transition, TransitionTimed):
                return ConflictGroupType.Timed
            return ConflictGroupType.Normal

        CGT = ConflictGroupType
        conflict_group_data = [None for _ in conflict_groups_sets]
        for cg_i, cg in enumerate(conflict_groups_sets):
            # cg type prefered by the transition
            t_types = [t_cg_type(t) for t in cg]

            if all(tt == CGT.Normal for tt in t_types):
                cg_type = CGT.Normal
            elif all(tt == CGT.Normal or tt == CGT.Priority for tt in t_types):
                # priority can be mixed with Normal
                cg_type = CGT.Priority
                conflict_group_data[cg_i] = np.zeros(len(self.transitions), dtype=np.uint)
            elif all(tt == CGT.Normal or tt == CGT.Timed for tt in t_types):
                # Timed can be mixed with Normal
                cg_type = CGT.Timed
                conflict_group_data[cg_i] = np.zeros((2, len(self.transitions)), dtype=np.bool)
            elif all(tt == CGT.Stochastic for tt in t_types):
                group_members_names = ', '.join([t.name for t in cg])
                # stochastic are on their own
                cg_type = CGT.Stochastic
                one_t_in_cg = next(iter(cg))
                ot_sources = set(i.source for i in one_t_in_cg.inputs)
                if not all(set(i.source for i in t.inputs) == ot_sources for t in cg):
                    raise RuntimeError('all members of stochastic group must share the same inputs: '+group_members_names)

                # TODO: maybe optional feature - all transitions in stochastic group might be required to take same amount of tokens?
                #if not all(t.inputs.n_tokens == one_t_in_cg.inputs.n_tokens for t in cg):
                #    RuntimeError('all members of stochastic group must take same number of tokens:'+group_members_names)

                conflict_group_data[cg_i] = np.zeros(len(self.transitions))
            else:
                raise RuntimeError('Unsupported combination of transitions: '+', '.join([str(tt) for tt in t_types]))

            conflict_groups_types[cg_i] = cg_type

        self.conflict_groups_waiting = np.zeros(len(conflict_groups_sets))
        self.conflict_groups_sets = tuple(tuple(cg) for cg in conflict_groups_sets)
        self.conflict_groups_types = tuple(conflict_groups_types)
        self.conflict_groups_mask = np.zeros((len(conflict_groups_sets), len(self.transitions)), dtype=np.bool)
        self.enabled_conflict_groups = np.zeros((len(conflict_groups_sets), len(self.transitions)), dtype=np.bool)
        for cgi, (cg, cgt) in enumerate(zip(conflict_groups_sets, conflict_groups_types)):
            for ti, t in enumerate(self.transitions):
                t_in_cg = t in cg
                self.conflict_groups_mask[cgi, ti] = t_in_cg

                if t_in_cg:
                    if cgt == CGT.Priority:
                        conflict_group_data[cgi][ti] = t.priority if hasattr(t, 'priority') else 0
                    elif cgt == CGT.Timed:
                        conflict_group_data[cgi][0, ti] = isinstance(t, TransitionTimed)
                        conflict_group_data[cgi][1, ti] = not isinstance(t, TransitionTimed)
                    elif cgt == CGT.Stochastic:
                        conflict_group_data[cgi][ti] = t.probability

        self.conflict_group_data = tuple(conflict_group_data)
