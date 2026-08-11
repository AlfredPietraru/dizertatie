the important thing, in order to generate correct json entries that can be consumed by the templater for launch files: first I need to properly understand the relationship between parameters, otherwise, it will be completely impossible to render valid .launch.py files

## Questions and Issues covert the YAML files to templates:
simple conversion between the yaml files to template is most probably incorrect.
The templates should be aware of dependencies and contradictions between the parameters.
How do I do that? The LLM clearly needs deep code understanding. 

Do I have to identify the parameters from the yaml while only looking in the code:
For example for odom_eval_params.yaml -> i have to look in odom_eval_node.py and find the correct
parameters associated with it and only from that build the yaml?







### Simplest solution: Pass the entire codebase to LLM
Let the LLM undertand on it's own all the dependencies between all the variables, and can update the yaml templates with all the things it might need. 
What it needs?  
Example: Constrains (thresholds or recommended entries), default values, dependencies to other parameters.

Source root                             Files          Bytes         Tokens
------------------------------------ -------- -------------- --------------
src/antrobot_ros                           69        339,620         81,128
src/antrobot_description                   20         52,184         14,478
src/kiss-icp                              111        352,631         87,560
src/kinematic-icp                          47        257,755         62,058
TOTAL                                     247      1,002,190        245,224

And the model I am currently using has a context window of 131072 tokens, which is almost half the amount. While ignoring the fact that passing all the content might be a bad solution it itself due to context rot. Also there is a limitation on the output size which could be only up to 32K tokens.
So the input should be around 90K - 110K tokens.
Some context engineering work is necessary.

### A better solution - pass to the LLM for a particular parameter only the chunks of code relevant to that parameter, greatly reduces the context.
I build the code graph -> what is next.
Need to define tools to call


Ollama, MCP -> need to better understand them and  