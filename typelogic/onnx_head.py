"""Export the actual trained head; validate numerical parity before accepting it."""
from pathlib import Path
import numpy as np

def export_pointer(head,output=None):
    """Build the readout graph; save it when ``output`` is given and return serialized bytes."""
    import onnx
    from onnx import helper,numpy_helper,TensorProto
    params={'qw':head.q.weight.detach().cpu().numpy().T,'qb':head.q.bias.detach().cpu().numpy(),
            'kw':head.k.weight.detach().cpu().numpy().T,'kb':head.k.bias.detach().cpu().numpy(),
            'scale':np.asarray(head.scale/head.temperature,dtype=np.float32)}
    nodes=[helper.make_node('MatMul',['decision','qw'],['q0']),helper.make_node('Add',['q0','qb'],['q']),
           helper.make_node('MatMul',['options','kw'],['k0']),helper.make_node('Add',['k0','kb'],['k']),
           helper.make_node('MatMul',['k','q'],['dot']),helper.make_node('Mul',['dot','scale'],['logits'])]
    dim=head.q.in_features
    graph=helper.make_graph(nodes,'typed-decision-readout',[helper.make_tensor_value_info('decision',TensorProto.FLOAT,[dim]),helper.make_tensor_value_info('options',TensorProto.FLOAT,['options_count',dim])],[helper.make_tensor_value_info('logits',TensorProto.FLOAT,['options_count'])],[numpy_helper.from_array(v.astype(np.float32),k) for k,v in params.items()])
    model=helper.make_model(graph,opset_imports=[helper.make_opsetid('',17)],producer_name='TypeLogic AI');model.ir_version=9
    onnx.checker.check_model(model)
    if output is not None:
        output=Path(output);output.parent.mkdir(parents=True,exist_ok=True);onnx.save(model,output)
    return model.SerializeToString()

def convert(head,graph):
    """Load a graph from a path or serialized bytes and require numerical parity with ``head``."""
    from .offline import install
    install()
    import torch
    import onnxruntime as ort
    ort.disable_telemetry_events()
    options=ort.SessionOptions();options.intra_op_num_threads=1
    session=ort.InferenceSession(graph if isinstance(graph,bytes) else str(graph),options,providers=['CPUExecutionProvider'])
    rng=np.random.default_rng(7913);largest=0.
    for count in (2,5,17):
        decision=rng.standard_normal(head.q.in_features).astype(np.float32);items=rng.standard_normal((count,head.q.in_features)).astype(np.float32)
        with torch.no_grad():expected=head(torch.from_numpy(decision),torch.from_numpy(items)).numpy()
        actual=session.run(None,{'decision':decision,'options':items})[0]
        np.testing.assert_allclose(actual,expected,rtol=3e-5,atol=3e-5);largest=max(largest,float(np.max(np.abs(actual-expected))))
    class Readout(torch.nn.Module):
        def __init__(self):super().__init__();self.temperature=head.temperature
        def forward(self,decision,items):
            value=session.run(None,{'decision':decision.detach().cpu().float().numpy(),'options':items.detach().cpu().float().numpy()})[0]
            return torch.from_numpy(value).to(decision.device)
    return Readout(),{'runtime':ort.__version__,'providers':session.get_providers(),'max_abs_logit_difference':largest,'telemetry_disabled':True}
