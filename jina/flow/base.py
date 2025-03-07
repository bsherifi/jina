__copyright__ = "Copyright (c) 2020 Jina AI Limited. All rights reserved."
__license__ = "Apache-2.0"

import argparse
import base64
import copy
import os
import re
import threading
import uuid
from collections import OrderedDict, defaultdict
from contextlib import ExitStack
from typing import Optional, Union, Tuple, List, Set, Dict, TextIO

from .builder import build_required, _build_flow, _hanging_pods
from .. import __default_host__
from ..clients import Client, WebSocketClient
from ..enums import FlowBuildLevel, PodRoleType, FlowInspectType
from ..excepts import FlowTopologyError, FlowMissingPodError
from ..helper import (
    colored,
    get_public_ip,
    get_internal_ip,
    typename,
    ArgNamespace,
    download_mermaid_url,
)
from ..jaml import JAML, JAMLCompatible
from ..logging import JinaLogger
from ..parsers import set_client_cli_parser, set_gateway_parser, set_pod_parser

__all__ = ['BaseFlow']

from ..peapods import BasePod


class FlowType(type(ExitStack), type(JAMLCompatible)):
    """Type of Flow, metaclass of :class:`BaseFlow`"""

    pass


_regex_port = r'(.*?):([0-9]{1,4}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])$'


class BaseFlow(JAMLCompatible, ExitStack, metaclass=FlowType):
    """An abstract Flow object in Jina.

    .. note::

        :class:`BaseFlow` does not provide `train`, `index`, `search` interfaces.
        Please use :class:`Flow` or :class:`AsyncFlow`.

    Explanation on ``optimize_level``:

    As an example, the following Flow will generate 6 Peas,

    .. highlight:: python
    .. code-block:: python

        f = Flow.add(uses='forward', parallel=3)

    :param kwargs: other keyword arguments that will be shared by all Pods in this Flow
    :param args: Namespace args
    :param env: environment variables shared by all Pods
    """

    _cls_client = Client  #: the type of the Client, can be changed to other class

    def __init__(
        self,
        args: Optional['argparse.Namespace'] = None,
        env: Optional[Dict] = None,
        **kwargs,
    ):
        super().__init__()
        self._version = '1'  #: YAML version number, this will be later overridden if YAML config says the other way
        self._pod_nodes = OrderedDict()  # type: Dict[str, 'BasePod']
        self._inspect_pods = {}  # type: Dict[str, str]
        self._build_level = FlowBuildLevel.EMPTY
        self._last_changed_pod = [
            'gateway'
        ]  #: default first pod is gateway, will add when build()
        self._update_args(args, **kwargs)
        self._env = env
        if isinstance(self.args, argparse.Namespace):
            self.logger = JinaLogger(self.__class__.__name__, **vars(self.args))
        else:
            self.logger = JinaLogger(self.__class__.__name__)

    def _update_args(self, args, **kwargs):
        from ..parsers.flow import set_flow_parser
        from ..helper import ArgNamespace

        _flow_parser = set_flow_parser()
        if args is None:
            args = ArgNamespace.kwargs2namespace(kwargs, _flow_parser)
        self.args = args
        self._common_kwargs = kwargs
        self._kwargs = ArgNamespace.get_non_defaults_args(
            args, _flow_parser
        )  #: for yaml dump

    @property
    def yaml_spec(self):
        """
        get the YAML representation of the instance


        .. # noqa: DAR401


        .. # noqa: DAR201
        """
        return JAML.dump(self)

    @staticmethod
    def _parse_endpoints(op_flow, pod_name, endpoint, connect_to_last_pod=False) -> Set:
        # parsing needs
        if isinstance(endpoint, str):
            endpoint = [endpoint]
        elif not endpoint:
            if op_flow._last_changed_pod and connect_to_last_pod:
                endpoint = [op_flow.last_pod]
            else:
                endpoint = []

        if isinstance(endpoint, (list, tuple)):
            for idx, s in enumerate(endpoint):
                if s == pod_name:
                    raise FlowTopologyError(
                        'the income/output of a pod can not be itself'
                    )
        else:
            raise ValueError(f'endpoint={endpoint} is not parsable')

        # if an endpoint is being inspected, then replace it with inspected Pod
        endpoint = set(op_flow._inspect_pods.get(ep, ep) for ep in endpoint)
        return endpoint

    @property
    def last_pod(self):
        """Last pod


        .. # noqa: DAR401


        .. # noqa: DAR201
        """
        return self._last_changed_pod[-1]

    @last_pod.setter
    def last_pod(self, name: str):
        """
        Set a Pod as the last Pod in the Flow, useful when modifying the Flow.


        .. # noqa: DAR401
        :param name: the name of the existing Pod
        """
        if name not in self._pod_nodes:
            raise FlowMissingPodError(f'{name} can not be found in this Flow')

        if self._last_changed_pod and name == self.last_pod:
            pass
        else:
            self._last_changed_pod.append(name)

        # graph is now changed so we need to
        # reset the build level to the lowest
        self._build_level = FlowBuildLevel.EMPTY

    def _add_gateway(self, needs, **kwargs):
        pod_name = 'gateway'

        kwargs.update(
            dict(
                name=pod_name,
                ctrl_with_ipc=True,  # otherwise ctrl port would be conflicted
                read_only=True,
                runtime_cls='GRPCRuntime',
                pod_role=PodRoleType.GATEWAY,
                identity=self.args.identity,
            )
        )

        kwargs.update(self._common_kwargs)
        args = ArgNamespace.kwargs2namespace(kwargs, set_gateway_parser())

        self._pod_nodes[pod_name] = BasePod(args, needs)

    def needs(
        self, needs: Union[Tuple[str], List[str]], name: str = 'joiner', *args, **kwargs
    ) -> 'BaseFlow':
        """
        Add a blocker to the Flow, wait until all peas defined in **needs** completed.


        .. # noqa: DAR401
        :param needs: list of service names to wait
        :param name: the name of this joiner, by default is ``joiner``
        :param *args: *args for .add
        :param **kwargs: **kwargs for .add
        :return: the modified Flow
        """
        if len(needs) <= 1:
            raise FlowTopologyError(
                'no need to wait for a single service, need len(needs) > 1'
            )
        return self.add(
            name=name, needs=needs, pod_role=PodRoleType.JOIN, *args, **kwargs
        )

    def needs_all(self, name: str = 'joiner', *args, **kwargs) -> 'BaseFlow':
        """
        Collect all hanging Pods so far and add a blocker to the Flow; wait until all handing peas completed.

        :param name: the name of this joiner (default is ``joiner``)
        :param *args: *args for .add or .needs
        :param **kwargs: **kwargs for .add or .needs
        :return: the modified Flow
        """
        needs = _hanging_pods(self)
        if len(needs) == 1:
            return self.add(name=name, needs=needs, *args, **kwargs)

        return self.needs(name=name, needs=needs, *args, **kwargs)

    def add(
        self,
        needs: Optional[Union[str, Tuple[str], List[str]]] = None,
        copy_flow: bool = True,
        pod_role: 'PodRoleType' = PodRoleType.POD,
        **kwargs,
    ) -> 'BaseFlow':
        """
        Add a Pod to the current Flow object and return the new modified Flow object.
        The attribute of the Pod can be later changed with :py:meth:`set` or deleted with :py:meth:`remove`

        Note there are shortcut versions of this method.
        Recommend to use :py:meth:`add_encoder`, :py:meth:`add_preprocessor`,
        :py:meth:`add_router`, :py:meth:`add_indexer` whenever possible.


        .. # noqa: DAR401
        :param needs: the name of the Pod(s) that this Pod receives data from.
                           One can also use 'pod.Gateway' to indicate the connection with the gateway.
        :param pod_role: the role of the Pod, used for visualization and route planning
        :param copy_flow: when set to true, then always copy the current Flow and do the modification on top of it then return, otherwise, do in-line modification
        :param **kwargs: other keyword-value arguments that the Pod CLI supports
        :return: a (new) Flow object with modification
        """

        op_flow = copy.deepcopy(self) if copy_flow else self

        # pod naming logic
        pod_name = kwargs.get('name', None)

        if pod_name in op_flow._pod_nodes:
            new_name = f'{pod_name}{len(op_flow._pod_nodes)}'
            self.logger.debug(
                f'"{pod_name}" is used in this Flow already! renamed it to "{new_name}"'
            )
            pod_name = new_name

        if not pod_name:
            pod_name = f'pod{len(op_flow._pod_nodes)}'

        if not pod_name.isidentifier():
            # hyphen - can not be used in the name
            raise ValueError(
                f'name: {pod_name} is invalid, please follow the python variable name conventions'
            )

        # needs logic
        needs = op_flow._parse_endpoints(
            op_flow, pod_name, needs, connect_to_last_pod=True
        )

        # set the kwargs inherit from `Flow(kwargs1=..., kwargs2=)`
        for key, value in op_flow._common_kwargs.items():
            if key not in kwargs:
                kwargs[key] = value

        # check if host is set to remote:port
        if 'host' in kwargs:
            m = re.match(_regex_port, kwargs['host'])
            if (
                kwargs.get('host', __default_host__) != __default_host__
                and m
                and 'port_expose' not in kwargs
            ):
                kwargs['port_expose'] = m.group(2)
                kwargs['host'] = m.group(1)

        # update kwargs of this Pod
        kwargs.update(dict(name=pod_name, pod_role=pod_role, num_part=len(needs)))

        parser = set_pod_parser()
        if pod_role == PodRoleType.GATEWAY:
            parser = set_gateway_parser()

        args = ArgNamespace.kwargs2namespace(kwargs, parser)

        op_flow._pod_nodes[pod_name] = BasePod(args, needs=needs)
        op_flow.last_pod = pod_name

        return op_flow

    def inspect(self, name: str = 'inspect', *args, **kwargs) -> 'BaseFlow':
        """Add an inspection on the last changed Pod in the Flow

        Internally, it adds two Pods to the Flow. But don't worry, the overhead is minimized and you
        can remove them by simply using `Flow(inspect=FlowInspectType.REMOVE)` before using the Flow.

        .. highlight:: bash
        .. code-block:: bash

            Flow -- PUB-SUB -- BasePod(_pass) -- Flow
                    |
                    -- PUB-SUB -- InspectPod (Hanging)

        In this way, :class:`InspectPod` looks like a simple ``_pass`` from outside and
        does not introduce side-effects (e.g. changing the socket type) to the original Flow.
        The original incoming and outgoing socket types are preserved.

        This function is very handy for introducing an Evaluator into the Flow.

        .. seealso::

            :meth:`gather_inspect`

        :param name: name of the Pod
        :param *args: *args for .add()
        :param **kwargs: **kwargs for .add()
        :return: the new instance of the Flow
        """
        _last_pod = self.last_pod
        op_flow = self.add(
            name=name, needs=_last_pod, pod_role=PodRoleType.INSPECT, *args, **kwargs
        )

        # now remove uses and add an auxiliary Pod
        if 'uses' in kwargs:
            kwargs.pop('uses')
        op_flow = op_flow.add(
            name=f'_aux_{name}',
            needs=_last_pod,
            pod_role=PodRoleType.INSPECT_AUX_PASS,
            *args,
            **kwargs,
        )

        # register any future connection to _last_pod by the auxiliary Pod
        op_flow._inspect_pods[_last_pod] = op_flow.last_pod

        return op_flow

    def gather_inspect(
        self,
        name: str = 'gather_inspect',
        uses='_merge_eval',
        include_last_pod: bool = True,
        *args,
        **kwargs,
    ) -> 'BaseFlow':
        """Gather all inspect Pods output into one Pod. When the Flow has no inspect Pod then the Flow itself
        is returned.

        .. note::

            If ``--no-inspect`` is **not** given, then :meth:`gather_inspect` is auto called before :meth:`build`. So
            in general you don't need to manually call :meth:`gather_inspect`.

        :param name: the name of the gather Pod
        :param uses: the config of the executor, by default is ``_pass``
        :param include_last_pod: if to include the last modified Pod in the Flow
        :param *args: *args for .add()
        :param **kwargs: **kwargs for .add()
        :return: the modified Flow or the copy of it


        .. seealso::

            :meth:`inspect`

        """
        needs = [k for k, v in self._pod_nodes.items() if v.role == PodRoleType.INSPECT]
        if needs:
            if include_last_pod:
                needs.append(self.last_pod)
            return self.add(
                name=name,
                uses=uses,
                needs=needs,
                pod_role=PodRoleType.JOIN_INSPECT,
                *args,
                **kwargs,
            )
        else:
            # no inspect node is in the graph, return the current graph
            return self

    def build(self, copy_flow: bool = False) -> 'BaseFlow':
        """
        Build the current Flow and make it ready to use

        .. note::

            No need to manually call it since 0.0.8. When using Flow with the
            context manager, or using :meth:`start`, :meth:`build` will be invoked.

        :param copy_flow: when set to true, then always copy the current Flow and do the modification on top of it then return, otherwise, do in-line modification
        :return: the current Flow (by default)

        .. note::
            ``copy_flow=True`` is recommended if you are building the same Flow multiple times in a row. e.g.

            .. highlight:: python
            .. code-block:: python

                f = Flow()
                with f:
                    f.index()

                with f.build(copy_flow=True) as fl:
                    fl.search()


        .. # noqa: DAR401
        """

        op_flow = copy.deepcopy(self) if copy_flow else self

        if op_flow.args.inspect == FlowInspectType.COLLECT:
            op_flow.gather_inspect(copy_flow=False)

        if 'gateway' not in op_flow._pod_nodes:
            op_flow._add_gateway(needs={op_flow.last_pod})

        # construct a map with a key a start node and values an array of its end nodes
        _outgoing_map = defaultdict(list)

        # if set no_inspect then all inspect related nodes are removed
        if op_flow.args.inspect == FlowInspectType.REMOVE:
            op_flow._pod_nodes = {
                k: v for k, v in op_flow._pod_nodes.items() if not v.role.is_inspect
            }
            reverse_inspect_map = {v: k for k, v in op_flow._inspect_pods.items()}

        for end, pod in op_flow._pod_nodes.items():
            # if an endpoint is being inspected, then replace it with inspected Pod
            # but not those inspect related node
            if op_flow.args.inspect.is_keep:
                pod.needs = set(
                    ep if pod.role.is_inspect else op_flow._inspect_pods.get(ep, ep)
                    for ep in pod.needs
                )
            else:
                pod.needs = set(reverse_inspect_map.get(ep, ep) for ep in pod.needs)

            for start in pod.needs:
                if start not in op_flow._pod_nodes:
                    raise FlowMissingPodError(
                        f'{start} is not in this flow, misspelled name?'
                    )
                _outgoing_map[start].append(end)

        op_flow = _build_flow(op_flow, _outgoing_map)
        hanging_pods = _hanging_pods(op_flow)
        if hanging_pods:
            self.logger.warning(
                f'{hanging_pods} are hanging in this flow with no pod receiving from them, '
                f'you may want to double check if it is intentional or some mistake'
            )
        op_flow._build_level = FlowBuildLevel.GRAPH
        self._update_client()
        return op_flow

    def __call__(self, *args, **kwargs):
        """Builds the Flow
        :param *args: *args for build
        :param **kwargs: **kwargs for build
        :return: the built Flow
        """
        return self.build(*args, **kwargs)

    def __enter__(self):
        class CatchAllCleanupContextManager:
            """
            This context manager guarantees, that the :method:``__exit__`` of the
            sub context is called, even when there is an Exception in the
            :method:``__enter__``.

            :param sub_context: The context, that should be taken care of.
            """

            def __init__(self, sub_context):
                self.sub_context = sub_context

            def __enter__(self):
                pass

            def __exit__(self, exc_type, exc_val, exc_tb):
                if exc_type is not None:
                    self.sub_context.__exit__(exc_type, exc_val, exc_tb)

        with CatchAllCleanupContextManager(self):
            return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        super().__exit__(exc_type, exc_val, exc_tb)

        # unset all envs to avoid any side-effect
        if self._env:
            for k in self._env.keys():
                os.unsetenv(k)
        if 'gateway' in self._pod_nodes:
            self._pod_nodes.pop('gateway')
        self._build_level = FlowBuildLevel.EMPTY
        self.logger.success(
            f'flow is closed and all resources are released, current build level is {self._build_level}'
        )
        self.logger.close()

    def start(self):
        """Start to run all Pods in this Flow.

        Remember to close the Flow with :meth:`close`.

        Note that this method has a timeout of ``timeout_ready`` set in CLI,
        which is inherited all the way from :class:`jina.peapods.peas.BasePea`


        .. # noqa: DAR401

        :return: this instance
        """

        if self._build_level.value < FlowBuildLevel.GRAPH.value:
            self.build(copy_flow=False)

        # set env only before the Pod get started
        if self._env:
            for k, v in self._env.items():
                os.environ[k] = str(v)

        for k, v in self:
            v.args.noblock_on_start = True
            self.enter_context(v)

        for k, v in self:
            try:
                v.wait_start_success()
            except Exception as ex:
                self.logger.error(
                    f'{k}:{v!r} can not be started due to {ex!r}, Flow is aborted'
                )
                self.close()
                raise

        self.logger.info(
            f'{self.num_pods} Pods (i.e. {self.num_peas} Peas) are running in this Flow'
        )

        self._show_success_message()

        return self

    @property
    def num_pods(self) -> int:
        """Get the number of Pods in this Flow


        .. # noqa: DAR201"""
        return len(self._pod_nodes)

    @property
    def num_peas(self) -> int:
        """Get the number of peas (parallel count) in this Flow


        .. # noqa: DAR201"""
        return sum(v.num_peas for v in self._pod_nodes.values())

    def __eq__(self, other: 'BaseFlow') -> bool:
        """
        Compare the topology of a Flow with another Flow.
        Identification is defined by whether two flows share the same set of edges.

        :param other: the second Flow object
        :return: result of equality check
        """

        if self._build_level.value < FlowBuildLevel.GRAPH.value:
            a = self.build()
        else:
            a = self

        if other._build_level.value < FlowBuildLevel.GRAPH.value:
            b = other.build()
        else:
            b = other

        return a._pod_nodes == b._pod_nodes

    @build_required(FlowBuildLevel.GRAPH)
    def _get_client(self, **kwargs) -> 'Client':
        kwargs.update(self._common_kwargs)
        if 'port_expose' not in kwargs:
            kwargs['port_expose'] = self.port_expose
        if 'host' not in kwargs:
            kwargs['host'] = self.host

        args = ArgNamespace.kwargs2namespace(kwargs, set_client_cli_parser())
        return self._cls_client(args)

    @property
    def _mermaid_str(self):
        mermaid_graph = [
            "%%{init: {'theme': 'base', "
            "'themeVariables': { 'primaryColor': '#32C8CD', "
            "'edgeLabelBackground':'#fff', 'clusterBkg': '#FFCC66'}}}%%",
            'graph LR',
        ]

        start_repl = {}
        end_repl = {}
        for node, v in self._pod_nodes.items():
            if not v.is_singleton and v.role != PodRoleType.GATEWAY:
                mermaid_graph.append(
                    f'subgraph sub_{node} ["{node} ({v.args.parallel})"]'
                )
                if v.is_head_router:
                    head_router = node + '_HEAD'
                    end_repl[node] = (head_router, '((fa:fa-random))')
                if v.is_tail_router:
                    tail_router = node + '_TAIL'
                    start_repl[node] = (tail_router, '((fa:fa-random))')

                p_r = '((%s))'
                p_e = '[[%s]]'
                for j in range(v.args.parallel):
                    r = node + (f'_{j}' if v.args.parallel > 1 else '')
                    if v.is_head_router:
                        mermaid_graph.append(
                            f'\t{head_router}{p_r % "head"}:::pea-->{r}{p_e % r}:::pea'
                        )
                    if v.is_tail_router:
                        mermaid_graph.append(
                            f'\t{r}{p_e % r}:::pea-->{tail_router}{p_r % "tail"}:::pea'
                        )
                mermaid_graph.append('end')

        for node, v in self._pod_nodes.items():
            ed_str = str(v.head_args.socket_in).split('_')[0]
            for need in sorted(v.needs):
                edge_str = ''
                if need in self._pod_nodes:
                    st_str = str(self._pod_nodes[need].tail_args.socket_out).split('_')[
                        0
                    ]
                    edge_str = f'|{st_str}-{ed_str}|'

                _s = start_repl.get(need, (need, f'({need})'))
                _e = end_repl.get(node, (node, f'({node})'))
                _s_role = self._pod_nodes[need].role
                _e_role = self._pod_nodes[node].role
                line_st = '-->'

                if _s_role in {PodRoleType.INSPECT, PodRoleType.JOIN_INSPECT}:
                    _s = start_repl.get(need, (need, f'{{{{{need}}}}}'))

                if _e_role == PodRoleType.GATEWAY:
                    _e = ('gateway_END', f'({node})')
                elif _e_role in {PodRoleType.INSPECT, PodRoleType.JOIN_INSPECT}:
                    _e = end_repl.get(node, (node, f'{{{{{node}}}}}'))

                if _s_role == PodRoleType.INSPECT or _e_role == PodRoleType.INSPECT:
                    line_st = '-.->'

                mermaid_graph.append(
                    f'{_s[0]}{_s[1]}:::{str(_s_role)} {line_st} {edge_str}{_e[0]}{_e[1]}:::{str(_e_role)}'
                )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.POD)} fill:#32C8CD,stroke:#009999'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.INSPECT)} fill:#ff6666,color:#fff'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.JOIN_INSPECT)} fill:#ff6666,color:#fff'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.GATEWAY)} fill:#6E7278,color:#fff'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.INSPECT_AUX_PASS)} fill:#fff,color:#000,stroke-dasharray: 5 5'
        )
        mermaid_graph.append('classDef pea fill:#009999,stroke:#1E6E73')
        return '\n'.join(mermaid_graph)

    def plot(
        self,
        output: Optional[str] = None,
        vertical_layout: bool = False,
        inline_display: bool = False,
        build: bool = True,
        copy_flow: bool = False,
    ) -> 'BaseFlow':
        """
        Visualize the Flow up to the current point
        If a file name is provided it will create a jpg image with that name,
        otherwise it will display the URL for mermaid.
        If called within IPython notebook, it will be rendered inline,
        otherwise an image will be created.

        Example,

        .. highlight:: python
        .. code-block:: python

            flow = Flow().add(name='pod_a').plot('flow.svg')

        :param output: a filename specifying the name of the image to be created,
                    the suffix svg/jpg determines the file type of the output image
        :param vertical_layout: top-down or left-right layout
        :param inline_display: show image directly inside the Jupyter Notebook
        :param build: build the Flow first before plotting, gateway connection can be better showed
        :param copy_flow: when set to true, then always copy the current Flow and
                do the modification on top of it then return, otherwise, do in-line modification
        :return: the Flow
        """

        # deepcopy causes the below error while reusing a Flow in Jupyter
        # 'Pickling an AuthenticationString object is disallowed for security reasons'
        op_flow = copy.deepcopy(self) if copy_flow else self

        if build:
            op_flow.build(False)

        mermaid_str = op_flow._mermaid_str
        if vertical_layout:
            mermaid_str = mermaid_str.replace('graph LR', 'graph TD')

        image_type = 'svg'
        if output and output.endswith('jpg'):
            image_type = 'jpg'

        url = op_flow._mermaid_to_url(mermaid_str, image_type)
        showed = False
        if inline_display:
            try:
                from IPython.display import display, Image

                display(Image(url=url))
                showed = True
            except:
                # no need to panic users
                pass

        if output:
            download_mermaid_url(url, output)
        elif not showed:
            op_flow.logger.info(f'flow visualization: {url}')

        return self

    def _ipython_display_(self):
        """Displays the object in IPython as a side effect"""
        self.plot(
            inline_display=True, build=(self._build_level != FlowBuildLevel.GRAPH)
        )

    def _mermaid_to_url(self, mermaid_str: str, img_type: str) -> str:
        """
        Render the current Flow as URL points to a SVG. It needs internet connection

        :param mermaid_str: the mermaid representation
        :param img_type: image type (svg/jpg)
        :return: the url points to a SVG
        """
        if img_type == 'jpg':
            img_type = 'img'

        encoded_str = base64.b64encode(bytes(mermaid_str, 'utf-8')).decode('utf-8')

        return f'https://mermaid.ink/{img_type}/{encoded_str}'

    @build_required(FlowBuildLevel.GRAPH)
    def to_swarm_yaml(self, path: TextIO):
        """
        Generate the docker swarm YAML compose file

        :param path: the output yaml path
        """
        swarm_yml = {'version': '3.4', 'services': {}}

        for k, v in self._pod_nodes.items():
            if v.role == PodRoleType.GATEWAY:
                cmd = 'jina gateway'
            else:
                cmd = 'jina pod'
            swarm_yml['services'][k] = {
                'command': f'{cmd} {" ".join(ArgNamespace.kwargs2list(vars(v.args)))}',
                'deploy': {'parallel': 1},
            }

        JAML.dump(swarm_yml, path)

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def port_expose(self) -> int:
        """Return the exposed port of the gateway


        .. # noqa: DAR201"""
        return self._pod_nodes['gateway'].port_expose

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def host(self) -> str:
        """Return the local address of the gateway


        .. # noqa: DAR201"""
        return self._pod_nodes['gateway'].host

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def address_private(self) -> str:
        """Return the private IP address of the gateway for connecting from other machine in the same network


        .. # noqa: DAR201"""
        return get_internal_ip()

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def address_public(self) -> str:
        """Return the public IP address of the gateway for connecting from other machine in the public network


        .. # noqa: DAR201"""
        return get_public_ip()

    def __iter__(self):
        return self._pod_nodes.items().__iter__()

    def _show_success_message(self):
        if self._pod_nodes['gateway'].args.restful:
            header = 'http://'
            protocol = 'REST'
        else:
            header = 'tcp://'
            protocol = 'gRPC'

        address_table = [
            f'\t🖥️ Local access:\t'
            + colored(
                f'{header}{self.host}:{self.port_expose}', 'cyan', attrs='underline'
            ),
            f'\t🔒 Private network:\t'
            + colored(
                f'{header}{self.address_private}:{self.port_expose}',
                'cyan',
                attrs='underline',
            ),
        ]
        if self.address_public:
            address_table.append(
                f'\t🌐 Public address:\t'
                + colored(
                    f'{header}{self.address_public}:{self.port_expose}',
                    'cyan',
                    attrs='underline',
                )
            )
        self.logger.success(
            f'🎉 Flow is ready to use, accepting {colored(protocol + " request", attrs="bold")}'
        )
        self.logger.info('\n' + '\n'.join(address_table))

    def block(self):
        """Block the process until user hits KeyboardInterrupt """
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass

    def use_grpc_gateway(self, port: Optional[int] = None):
        """Change to use gRPC gateway for IO
        :param port: the port to change"""
        self._common_kwargs['restful'] = False
        if port:
            self._common_kwargs['port_expose'] = port

    def use_rest_gateway(self, port: Optional[int] = None):
        """Change to use REST gateway for IO
        :param port: the port to change"""
        self._common_kwargs['restful'] = True
        if port:
            self._common_kwargs['port_expose'] = port

    def __getitem__(self, item):
        if isinstance(item, str):
            return self._pod_nodes[item]
        elif isinstance(item, int):
            return list(self._pod_nodes.values())[item]
        else:
            raise TypeError(f'{typename(item)} is not supported')

    def _update_client(self):
        if self._pod_nodes['gateway'].args.restful:
            self._cls_client = WebSocketClient

    @property
    def workspace_id(self) -> Dict[str, str]:
        """Get all Pods' ``workspace_id`` values in a dict


        .. # noqa: DAR201"""
        return {
            k: p.args.workspace_id for k, p in self if hasattr(p.args, 'workspace_id')
        }

    @workspace_id.setter
    def workspace_id(self, value: str):
        """Set all Pods' ``workspace_id`` to ``value``

        :param value: a hexadecimal UUID string
        """
        uuid.UUID(value)
        for k, p in self:
            if hasattr(p.args, 'workspace_id'):
                p.args.workspace_id = value
                for k, v in p.peas_args.items():
                    if v and isinstance(v, argparse.Namespace):
                        v.workspace_id = value
                    if v and isinstance(v, List):
                        for i in v:
                            i.workspace_id = value

    @property
    def identity(self) -> Dict[str, str]:
        """Get all Pods' ``identity`` values in a dict


        .. # noqa: DAR201
        """
        return {k: p.args.identity for k, p in self}

    @identity.setter
    def identity(self, value: str):
        """Set all Pods' ``identity`` to ``value``

        :param value: a hexadecimal UUID string
        """
        uuid.UUID(value)
        self.args.identity = value
        # Re-initiating logger with new identity
        self.logger = JinaLogger(self.__class__.__name__, **vars(self.args))
        for _, p in self:
            p.args.identity = value

    # for backward support
    join = needs
